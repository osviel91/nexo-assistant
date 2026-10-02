from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable

from app.kernel import ToolDefinition, ToolExecutionContext


@dataclass(frozen=True)
class ToolCatalogEntry:
    name: str
    description: str
    parameters: dict[str, Any]
    source: str
    module_id: str | None
    capabilities: tuple[str, ...]
    action: str | None

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy({
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "source": self.source,
            "module_id": self.module_id,
            "capabilities": list(self.capabilities),
            "action": self.action,
        })


@dataclass(frozen=True)
class EffectiveToolSet:
    """Immutable tool snapshot used by one AgentRun."""

    _tools: tuple[ToolDefinition, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_tools",
            tuple(
                ToolDefinition(
                    tool.name,
                    tool.description,
                    copy.deepcopy(tool.parameters),
                    tool.handler,
                    tool.source,
                    tool.module_id,
                    tool.capabilities,
                    tool.action,
                )
                for tool in self._tools
            ),
        )

    @property
    def names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self._tools)

    def contains(self, name: str) -> bool:
        return any(tool.name == name for tool in self._tools)

    def definition(self, name: str) -> ToolDefinition | None:
        return next((tool for tool in self._tools if tool.name == name), None)

    def definitions(self) -> list[dict[str, Any]]:
        return copy.deepcopy([
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools
        ])


class ToolCatalog:
    """Read-only metadata and resolution view over registered tools."""

    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        self._tools = tuple(tools)

    def entries(self) -> tuple[ToolCatalogEntry, ...]:
        return tuple(
            ToolCatalogEntry(tool.name, tool.description, tool.parameters, tool.source, tool.module_id or None, tool.capabilities, tool.action)
            for tool in self._tools
        )

    def as_dicts(self) -> list[dict[str, Any]]:
        return [entry.as_dict() for entry in self.entries()]

    def resolve(self, requested_tool_names: Iterable[str] | None = None) -> EffectiveToolSet:
        requested = None if requested_tool_names is None else frozenset(requested_tool_names)
        return EffectiveToolSet(tuple(tool for tool in self._tools if requested is None or tool.name in requested))


class ExposurePolicy:
    """Minimal current policy: tool calling capability plus optional name intersections."""

    def resolve(
        self,
        catalog: ToolCatalog,
        capabilities: set[str],
        requested_tool_names: Iterable[str] | None = None,
        allowed_tool_names: Iterable[str] | None = None,
    ) -> EffectiveToolSet:
        if "tool-calling" not in capabilities:
            return EffectiveToolSet()
        requested = None if requested_tool_names is None else frozenset(requested_tool_names)
        allowed = None if allowed_tool_names is None else frozenset(allowed_tool_names)
        catalog_names = frozenset(entry.name for entry in catalog.entries())
        names = None if requested is None and allowed is None else (
            (requested if requested is not None else catalog_names)
            & (allowed if allowed is not None else catalog_names)
        )
        return catalog.resolve(names)


class ToolNotAvailableError(Exception):
    pass


class ToolExecutor:
    """Invokes only tools in the run's EffectiveToolSet."""

    async def invoke(
        self,
        effective_tools: EffectiveToolSet,
        name: str,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        tool = effective_tools.definition(name)
        if tool is None:
            raise ToolNotAvailableError(name)
        return await tool.handler(context, arguments)
