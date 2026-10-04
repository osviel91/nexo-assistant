from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta
from typing import Any, Callable


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).casefold()


def _record_score(value: Any, query: str) -> int:
    query_terms = set(re.findall(r"[\wáéíóúüñ]+", query.casefold()))
    text = _text(value)
    score = sum(term in text for term in query_terms)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    if "mañana" in query.casefold() or "manana" in query.casefold():
        score += 20 if tomorrow in text else 0
    if "pasado mañana" in query.casefold() or "pasado manana" in query.casefold():
        score += 20 if (date.today() + timedelta(days=2)).isoformat() in text else 0
    return score


def _compact(value: Any, query: str, budget: int, counts: dict[str, int], key: str = "") -> Any:
    if isinstance(value, dict):
        result = {}
        # Keep schema/identity/forecast fields ahead of bulky payloads.
        priority = ("id", "name", "nombre", "title", "titulo", "path", "date", "fecha", "municipio", "provincia", "temperatura", "precipitacion", "prob_precipitacion", "estado_cielo", "viento", "humedad", "score", "snippet", "description", "schema", "columns", "rows", "results", "records", "items", "metadata", "count", "total", "total_records", "units", "timezone", "source")
        keys = sorted(value, key=lambda key: str(key).casefold() not in priority)
        for key in keys[:60]:
            item = value[key]
            key_text = str(key).casefold()
            if key_text in {"html", "map", "mapa", "image", "imagen", "base64"}:
                counts["truncated"] = True
                continue
            if isinstance(item, str) and (len(item) > 500 or item.startswith("data:")):
                if item.startswith("data:"):
                    counts["truncated"] = True
                    continue
                item = item[:500] + "…"
            child_budget = max(80, min(budget // max(1, min(len(value), 12)), 1800))
            result[key] = _compact(item, query, child_budget, counts, key_text)
        return result
    if isinstance(value, list):
        records = key in {"rows", "results", "records", "items", "forecasts", "data"}
        if records:
            counts["original_record_count"] += len(value)
        ranked = sorted(enumerate(value), key=lambda pair: (-_record_score(pair[1], query), pair[0]))
        result = []
        for _, item in ranked[:min(len(ranked), 100)]:
            candidate = _compact(item, query, max(100, min(1200, budget // 5)), counts)
            if _json_size(result + [candidate]) > budget - 40:
                break
            result.append(candidate)
        if records:
            counts["projected_record_count"] += len(result)
        if len(result) < len(value):
            counts["truncated"] = True
        return result
    if isinstance(value, str):
        if value.startswith("data:") or (len(value) > 1000 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value)):
            counts["truncated"] = True
            return "[binary content omitted]"
        if len(value) > min(500, budget - 40):
            counts["truncated"] = True
            return value[:max(0, min(500, budget - 40))] + "…"
    return value


def aemet_projector(result: dict[str, Any], query: str, arguments: dict[str, Any], budget: int) -> dict[str, Any]:
    """Prioritize forecast records by request terms/date without changing the canonical result."""
    counts = {"original_record_count": 0, "projected_record_count": 0, "truncated": False}
    projected = _compact(copy.deepcopy(result), f"{query} {_text(arguments)}", budget, counts)
    projected.setdefault("result_metadata", {}).update(counts, total_records=counts["original_record_count"])
    return projected


class ToolResultPipeline:
    """Keep canonical results intact and build a bounded presentation for model context."""

    def __init__(self, max_chars: int = 12000) -> None:
        self.max_chars = max(100, max_chars)

    def process(self, raw: Any, *, query: str = "", arguments: dict[str, Any] | None = None,
                projector: Callable[[dict[str, Any], str, dict[str, Any], int], dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        result = raw if isinstance(raw, dict) else {"content": str(raw)}
        try:
            canonical = json.loads(json.dumps(result, ensure_ascii=False, default=str))
        except (TypeError, ValueError, OverflowError):
            canonical = {"content": "Tool returned data that could not be serialized."}
        original_size = _json_size(canonical)
        counts = {"original_record_count": 0, "projected_record_count": 0, "truncated": False}
        strategy = "text"
        structured = isinstance(canonical.get("structured_data", canonical.get("data")), (dict, list))
        if structured:
            strategy = "structured_tool_specific" if projector else "structured_generic"
        if original_size <= self.max_chars and not projector:
            return canonical, {"original_size": original_size, "projected_size": original_size, "compacted": False,
                              "truncated": False, "projection_strategy": strategy, **counts, "fields_preserved_count": 0}
        try:
            projected = projector(copy.deepcopy(canonical), query, arguments or {}, self.max_chars) if projector else _compact(copy.deepcopy(canonical), query, self.max_chars, counts)
            if not isinstance(projected, dict):
                raise TypeError("projector must return an object")
        except Exception:
            strategy = "structured_generic" if structured else "text"
            counts = {"original_record_count": 0, "projected_record_count": 0, "truncated": False}
            projected = _compact(copy.deepcopy(canonical), query, self.max_chars, counts)
        projection_counts = projected.get("result_metadata", {}) if isinstance(projected.get("result_metadata"), dict) else {}
        for key in ("original_record_count", "projected_record_count"):
            if isinstance(projection_counts.get(key), int):
                counts[key] = projection_counts[key]
        counts["truncated"] = counts["truncated"] or bool(projection_counts.get("truncated"))
        if _json_size(projected) > self.max_chars:
            projected = {"content": "Tool result exceeded context limit.", "result_metadata": {"truncated": True}}
            projected_size = _json_size(projected)
            return canonical, {"original_size": original_size, "projected_size": projected_size, "compacted": True,
                               "truncated": True, "projection": projected, "projection_strategy": strategy,
                               "original_record_count": counts["original_record_count"],
                               "projected_record_count": 0, "fields_preserved_count": 0}
        projected_size = _json_size(projected)
        truncated = bool(counts["truncated"] or projected_size < original_size)
        fields = projected.get("structured_data", projected.get("data"))
        metadata = projected.setdefault("result_metadata", {})
        metadata.update({"truncated": truncated, "original_record_count": counts["original_record_count"],
                         "projected_record_count": counts["projected_record_count"],
                         "total_records": counts["original_record_count"]})
        projected_size = _json_size(projected)
        if projected_size > self.max_chars:
            projected = {"content": "Tool result truncated.", "result_metadata": {"truncated": True}}
            projected_size = _json_size(projected)
            truncated = True
        return canonical, {"original_size": original_size, "projected_size": projected_size, "compacted": truncated,
                           "truncated": truncated, "projection": projected, "projection_strategy": strategy,
                           "original_record_count": counts["original_record_count"],
                           "projected_record_count": counts["projected_record_count"],
                           "fields_preserved_count": len(fields) if isinstance(fields, dict) else 0}
