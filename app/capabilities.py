from __future__ import annotations

from typing import Any


THINKING_CAPABILITIES = {"thinking", "thinking-budget", "reasoning-content"}


def thinking_capabilities(capabilities: Any) -> dict[str, bool]:
    values = {str(item) for item in capabilities} if isinstance(capabilities, (list, tuple, set)) else set()
    return {name: name in values for name in THINKING_CAPABILITIES}


def normalize_model_capabilities(model_payload: dict[str, Any], assume_tool_calling: bool = False) -> set[str]:
    raw = model_payload.get("capabilities", [])
    capabilities = {str(item) for item in raw} if isinstance(raw, list) else set()
    if model_payload.get("supports_tools") is True or model_payload.get("tool_calling") is True:
        capabilities.add("tool-calling")
    supported_parameters = model_payload.get("supported_parameters", [])
    if isinstance(supported_parameters, list) and any(
        "tool" in str(item).lower() or "function" in str(item).lower()
        for item in supported_parameters
    ):
        capabilities.add("tool-calling")
    if assume_tool_calling and "tool-calling" not in capabilities:
        capabilities.add("tool-calling")
    return capabilities


def preserve_model_capabilities(discovered: set[str], persisted: Any) -> set[str]:
    """Keep user-assigned capabilities when provider discovery runs again."""
    explicit = persisted if isinstance(persisted, list) else []
    return discovered | {str(item) for item in explicit if isinstance(item, str)}
