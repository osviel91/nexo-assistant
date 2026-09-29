from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from fastapi import FastAPI

from app.diagnostics import diagnostic

logger = logging.getLogger("nexo.kernel")
KERNEL_API_VERSION = 1
HookName = Literal["startup", "shutdown", "chat_before", "chat_after"]
@dataclass(frozen=True)
class ToolExecutionContext:
    conversation_id: str
    provider_id: str
    model_id: str
    round: int
    run_id: str | None = None


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
    source: str = "native"
    module_id: str = ""
    capabilities: tuple[str, ...] = ("tool-calling",)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if not tool.name or not isinstance(tool.parameters, dict) or tool.parameters.get("type") != "object":
            raise ValueError("tool requires a stable name and object input schema")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def definitions(
        self,
        context: ToolExecutionContext | None = None,
        capabilities: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        web_search = self._tools.get("web_search")
        diagnostic(logger, "tool_registry", **{
            "web_search_registered": web_search is not None,
            "schema_present": bool(web_search and web_search.parameters),
        })
        if capabilities is not None and "tool-calling" not in capabilities:
            return []
        return self.catalog_view().resolve().definitions()

    def catalog(self) -> list[dict[str, Any]]:
        return self.catalog_view().as_dicts()

    def registered_tools(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._tools.values())

    def has(self, name: str) -> bool:
        return name in self._tools

    def catalog_view(self):
        from app.tools import ToolCatalog

        return ToolCatalog(self.registered_tools())

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
    services: dict[str, Any] = field(default_factory=dict)


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
        catalog = []
        for module in self.modules.values():
            item = {
                "id": module.manifest.id,
                "name": module.manifest.name,
                "version": module.manifest.version,
                "capabilities": list(module.manifest.capabilities),
                "interface_extensions": [
                    {"id": ext.id, "kind": ext.kind, "label": ext.label}
                    for ext in module.interface_extensions
                ],
            }
            status = getattr(module, "catalog_status", None)
            if status:
                item["status"] = status()
            catalog.append(item)
        return catalog

    def tool_definitions(
        self,
        context: ToolExecutionContext | None = None,
        capabilities: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.context.tools.definitions(context, capabilities)

    def tool_catalog(self) -> list[dict[str, Any]]:
        return self.context.tools.catalog()

    def tool_catalog_view(self):
        return self.context.tools.catalog_view()

    async def invoke_tool(self, name: str, context: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.context.tools.invoke(name, context, arguments)

    def service(self, name: str) -> Any | None:
        return self.context.services.get(name)

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
