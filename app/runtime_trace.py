from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol


SAFE_METADATA_KEYS = {
    "model", "tool", "provider", "round", "status", "duration_ms",
    "error_code", "capabilities", "answers", "confidence", "probabilities",
}


def safe_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep observability metadata operational; never copy payload-shaped data."""
    output: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if key not in SAFE_METADATA_KEYS:
            continue
        if key in {"model", "tool", "provider", "status", "error_code"} and isinstance(value, (str, int, float, bool)):
            output[key] = str(value) if key != "round" else value
        elif key in {"round", "duration_ms"} and isinstance(value, (int, float)):
            output[key] = value
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
