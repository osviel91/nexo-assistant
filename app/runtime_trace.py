from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol


SAFE_METADATA_KEYS = {
    "model", "tool", "provider", "round", "status", "duration_ms",
    "error_code", "agent_profile_id", "capabilities", "answers", "confidence", "probabilities",
    "agent_profile_name", "resolved_provider", "resolved_model", "system_instructions_applied",
    "effective_tool_names", "temperature", "top_p", "top_k", "context_window", "context_used_tokens",
    "context_utilization", "prompt_tokens", "completion_tokens", "total_tokens", "ttft_ms",
    "generation_duration_ms", "total_duration_ms", "tokens_per_second", "usage_source",
    "tokens_per_second_source",
    "notebook_id", "retrieval_count", "retrieval_duration_ms", "context_chars", "context_truncated", "citation_count",
}


def safe_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep observability metadata operational; never copy payload-shaped data."""
    output: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if key not in SAFE_METADATA_KEYS:
            continue
        if key in {"model", "tool", "provider", "status", "error_code", "agent_profile_id", "agent_profile_name", "resolved_provider", "resolved_model", "notebook_id", "usage_source"} and isinstance(value, (str, int, float, bool)):
            output[key] = str(value)
        elif key == "system_instructions_applied" and isinstance(value, bool):
            output[key] = value
        elif key in {"round", "duration_ms", "retrieval_count", "retrieval_duration_ms", "context_chars", "citation_count", "temperature", "top_p", "top_k", "context_window", "context_used_tokens", "context_utilization", "prompt_tokens", "completion_tokens", "total_tokens", "ttft_ms", "generation_duration_ms", "total_duration_ms", "tokens_per_second"} and isinstance(value, (int, float)):
            output[key] = value
        elif key in {"context_truncated"} and isinstance(value, bool):
            output[key] = value
        elif key == "effective_tool_names" and isinstance(value, (list, tuple)):
            output[key] = [str(item) for item in value[:50] if isinstance(item, str)]
        elif key == "capabilities" and isinstance(value, (list, tuple)):
            output[key] = [str(item) for item in value[:20] if isinstance(item, (str, int, float))]
        elif key in {"confidence", "probabilities", "answers"} and isinstance(value, dict):
            # Decision shadow values are already structured operational observations.
            output[key] = {str(k): v for k, v in value.items() if isinstance(k, str)}
    return output


class RuntimeEventSink(Protocol):
    def start_event(self, kind: str, name: str, metadata: Mapping[str, Any] | None = None, parent_event_id: str | None = None) -> str | None: ...
    def finish_event(self, event_id: str | None, status: str, metadata: Mapping[str, Any] | None = None, duration_ms: float | None = None) -> None: ...


class NullRuntimeEventSink:
    def start_event(self, kind: str, name: str, metadata: Mapping[str, Any] | None = None, parent_event_id: str | None = None) -> None:
        return None

    def finish_event(self, event_id: str | None, status: str, metadata: Mapping[str, Any] | None = None, duration_ms: float | None = None) -> None:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
