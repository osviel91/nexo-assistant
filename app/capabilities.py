from __future__ import annotations

from typing import Any


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
