from __future__ import annotations

import json
from typing import Any


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


class ToolResultPipeline:
    """Normalize results and create a bounded, JSON-valid model projection."""

    def __init__(self, max_chars: int = 12000) -> None:
        self.max_chars = max(100, max_chars)

    def process(self, raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        result = raw if isinstance(raw, dict) else {"content": str(raw)}
        canonical = json.loads(json.dumps(result, ensure_ascii=False, default=str))
        original_size = _json_size(canonical)
        projected = json.loads(json.dumps(canonical, ensure_ascii=False))
        if _json_size(projected) <= self.max_chars:
            return canonical, {"original_size": original_size, "projected_size": _json_size(projected), "compacted": False, "truncated": False}

        data = projected.get("structured_data")
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            rows = data["rows"]
            total = len(rows)
            low, high = 0, total
            while low < high:
                mid = (low + high + 1) // 2
                data["rows"] = rows[:mid]
                projected["result_metadata"] = {"truncated": True, "total_records": total, "returned_records": mid, "original_size": original_size}
                if _json_size(projected) <= self.max_chars:
                    low = mid
                else:
                    high = mid - 1
            data["rows"] = rows[:low]
            projected["result_metadata"] = {"truncated": True, "total_records": total, "returned_records": low, "original_size": original_size}
            if _json_size(projected) <= self.max_chars:
                return canonical, {"original_size": original_size, "projected_size": _json_size(projected), "compacted": True, "truncated": True, "projection": projected}

        # Preserve object/field names and representative records by shrinking arrays.
        omitted = 0
        compacted_list: list[Any] | None = None
        compacted_total = 0
        table = projected.get("structured_data")
        if isinstance(table, dict) and isinstance(table.get("rows"), list):
            compacted_list = table["rows"]
            compacted_total = len(compacted_list)
            while len(compacted_list) > 1 and _json_size(projected) > self.max_chars - 140:
                keep = max(1, len(compacted_list) // 2)
                omitted += len(compacted_list) - keep
                compacted_list[:] = compacted_list[:keep]
        while _json_size(projected) > self.max_chars - 140:
            lists: list[tuple[int, list[Any]]] = []
            def collect(value: Any) -> None:
                if isinstance(value, dict):
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    lists.append((_json_size(value), value))
                    for child in value:
                        collect(child)
            collect(projected)
            candidates = [value for _, value in sorted(lists, key=lambda item: item[0], reverse=True) if len(value) > 1]
            if not candidates:
                break
            target = candidates[0]
            if compacted_list is None:
                compacted_list, compacted_total = target, len(target)
            keep = max(1, len(target) // 2)
            omitted += len(target) - keep
            target[:] = target[:keep]
        if omitted:
            projected.setdefault("result_metadata", {}).update({"truncated": True, "structured_data_compacted": True, "omitted_records": omitted, "total_records": compacted_total, "returned_records": len(compacted_list or [])})
        if _json_size(projected) > self.max_chars:
            content = projected.get("content")
            if isinstance(content, str):
                projected["content"] = content[:max(0, self.max_chars - 160)]
            # If structured data alone is too large, keep a valid bounded summary.
            if _json_size(projected) > self.max_chars:
                data = projected.get("structured_data")
                if isinstance(data, dict):
                    for key, value in list(data.items()):
                        if isinstance(value, list):
                            data[key] = value[:max(0, len(value) // 2)]
                    projected["result_metadata"] = {"truncated": True, "structured_data_compacted": True}
                if _json_size(projected) > self.max_chars:
                    projected = {"content": "Tool result exceeded the model context limit.", "result_metadata": {"truncated": True}}
        metadata = projected.setdefault("result_metadata", {})
        metadata.update({"truncated": True, "original_size": original_size, "projected_size": _json_size(projected)})
        while _json_size(projected) > self.max_chars:
            projected = {"content": "Tool result truncated.", "truncated": True}
        return canonical, {"original_size": original_size, "projected_size": _json_size(projected), "compacted": True, "truncated": True, "projection": projected}
