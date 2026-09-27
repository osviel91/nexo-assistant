from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from fastapi import FastAPI

logger = logging.getLogger("nexo.kernel")
KERNEL_API_VERSION = 1
HookName = Literal["startup", "shutdown", "chat_before", "chat_after"]
@dataclass(frozen=True)
class ToolExecutionContext:
    conversation_id: str
    provider_id: str
    model_id: str
    round: int


ToolHandler = Callable[[ToolExecutionContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ModuleManifest:
    id: str
    name: str
    version: str
    api_version: int
    capabilities: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterfaceExtension:
    id: str
    kind: str
    label: str


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def definitions(
        self,
        context: ToolExecutionContext | None = None,
        capabilities: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if capabilities is not None and "tool-calling" not in capabilities:
            return []
        return [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in self._tools.values()
        ]

    async def invoke(self, name: str, context: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(name)
        return await tool.handler(context, arguments)


@dataclass
class ModuleContext:
    app: FastAPI
    settings: dict[str, Any] = field(default_factory=dict)
    tools: ToolRegistry = field(default_factory=ToolRegistry)


class Module(Protocol):
    manifest: ModuleManifest
    interface_extensions: tuple[InterfaceExtension, ...]

    def register(self, context: ModuleContext) -> None: ...
    def startup(self, context: ModuleContext) -> None: ...
    def shutdown(self, context: ModuleContext) -> None: ...
    def run_hook(self, hook: HookName, context: ModuleContext, payload: Any) -> Any: ...


class ModuleRegistry:
    def __init__(self, app: FastAPI, settings: dict[str, Any] | None = None) -> None:
        self.context = ModuleContext(app, settings or {})
        self.modules: dict[str, Module] = {}
        self.diagnostics: list[str] = []

    def register(self, module: Module) -> bool:
        manifest = module.manifest
        if manifest.api_version != KERNEL_API_VERSION:
            return self._skip(manifest.id, f"incompatible kernel API {manifest.api_version}")
        if manifest.id in self.modules:
            return self._skip(manifest.id, "duplicate module id")
        missing = [dep for dep in manifest.dependencies if dep not in self.modules]
        if missing:
            return self._skip(manifest.id, f"missing dependencies: {', '.join(missing)}")
        try:
            module.register(self.context)
        except Exception as exc:
            return self._skip(manifest.id, f"register failed: {type(exc).__name__}")
        self.modules[manifest.id] = module
        return True

    def startup(self) -> None:
        self._run_lifecycle("startup")

    def shutdown(self) -> None:
        self._run_lifecycle("shutdown")

    def run_hook(self, hook: HookName, payload: Any = None) -> list[Any]:
        results = []
        for module in self.modules.values():
            try:
                results.append(module.run_hook(hook, self.context, payload))
            except Exception as exc:
                self._diagnose(module.manifest.id, f"{hook} hook failed: {type(exc).__name__}")
        return results

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": module.manifest.id,
                "name": module.manifest.name,
                "version": module.manifest.version,
                "capabilities": list(module.manifest.capabilities),
                "interface_extensions": [
                    {"id": ext.id, "kind": ext.kind, "label": ext.label}
                    for ext in module.interface_extensions
                ],
            }
            for module in self.modules.values()
        ]

    def tool_definitions(
        self,
        context: ToolExecutionContext | None = None,
        capabilities: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.context.tools.definitions(context, capabilities)

    async def invoke_tool(self, name: str, context: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.context.tools.invoke(name, context, arguments)

    def _run_lifecycle(self, hook: Literal["startup", "shutdown"]) -> None:
        for module in self.modules.values():
            try:
                getattr(module, hook)(self.context)
            except Exception as exc:
                self._diagnose(module.manifest.id, f"{hook} failed: {type(exc).__name__}")

    def _skip(self, module_id: str, reason: str) -> bool:
        self._diagnose(module_id, reason)
        return False

    def _diagnose(self, module_id: str, reason: str) -> None:
        message = f"module {module_id}: {reason}"
        self.diagnostics.append(message)
        logger.error(message)


def enabled_module_ids() -> set[str]:
    raw = os.getenv("NEXO_MODULES", "attachments")
    return {item.strip() for item in raw.split(",") if item.strip()}
