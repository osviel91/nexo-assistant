from __future__ import annotations

import json
import math
from typing import Any


FIELD_TYPES = {"text", "textarea", "number", "integer", "boolean", "select", "multiselect", "json"}


def normalize_dialog(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("dialog must be an object")
    title = _text(value.get("title"), "title", 120)
    message = _text(value.get("message", ""), "message", 1200, allow_empty=True)
    raw_fields = value.get("fields", [])
    if not isinstance(raw_fields, list) or len(raw_fields) > 12:
        raise ValueError("fields must contain at most 12 items")
    fields = []
    seen = set()
    for raw in raw_fields:
        if not isinstance(raw, dict):
            raise ValueError("each field must be an object")
        field_id = _text(raw.get("id"), "field id", 64)
        if not field_id.replace("_", "").replace("-", "").isalnum() or field_id in seen:
            raise ValueError("field ids must be unique and use letters, digits, _ or -")
        seen.add(field_id)
        kind = raw.get("type", "text")
        if kind not in FIELD_TYPES:
            raise ValueError("unsupported field type")
        field = {"id": field_id, "label": _text(raw.get("label", field_id), "field label", 120), "type": kind,
                 "required": raw.get("required", False) is True}
        for key, limit in (("placeholder", 160), ("help", 240)):
            if key in raw:
                field[key] = _text(raw[key], key, limit, allow_empty=True)
        if kind in {"select", "multiselect"}:
            options = raw.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= 30:
                raise ValueError("choice fields require 1-30 options")
            field["options"] = [{"label": _text(item.get("label"), "option label", 120), "value": _scalar(item.get("value"))}
                                if isinstance(item, dict) else {"label": _text(item, "option", 120), "value": _scalar(item)}
                                for item in options]
        if "default" in raw:
            field["default"] = raw["default"]
        for key in ("min", "max"):
            if key in raw:
                if not isinstance(raw[key], (int, float)) or isinstance(raw[key], bool):
                    raise ValueError(f"{key} must be numeric")
                if isinstance(raw[key], float) and not math.isfinite(raw[key]):
                    raise ValueError(f"{key} must be finite")
                field[key] = raw[key]
        if "default" in field:
            validate_values([{**field, "required": False}], {field_id: field["default"]})
        fields.append(field)
    result = {"title": title, "message": message, "submit_label": _text(value.get("submit_label", "Continue"), "submit_label", 40), "fields": fields}
    _check_json_size(result, 24000)
    return result


def validate_values(fields: list[dict[str, Any]], values: Any) -> dict[str, Any]:
    if not isinstance(values, dict) or set(values) - {field["id"] for field in fields}:
        raise ValueError("unexpected form values")
    result = {}
    for field in fields:
        key, kind = field["id"], field["type"]
        value = values.get(key, field.get("default"))
        if value is None or value == "" or kind == "multiselect" and value == []:
            if field["required"]:
                raise ValueError(f"{field['label']} is required")
            continue
        if kind in {"text", "textarea"}:
            if not isinstance(value, str) or len(value) > 4000:
                raise ValueError(f"{field['label']} must be text up to 4000 characters")
        elif kind in {"number", "integer"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "integer" and not isinstance(value, int)):
                raise ValueError(f"{field['label']} must be {kind}")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{field['label']} must be finite")
            if "min" in field and value < field["min"] or "max" in field and value > field["max"]:
                raise ValueError(f"{field['label']} is outside its allowed range")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{field['label']} must be boolean")
        elif kind == "select":
            if value not in [option["value"] for option in field["options"]]:
                raise ValueError(f"{field['label']} is not an available option")
        elif kind == "multiselect":
            allowed = [option["value"] for option in field["options"]]
            if not isinstance(value, list) or any(item not in allowed for item in value):
                raise ValueError(f"{field['label']} contains an unavailable option")
        elif kind == "json":
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, RecursionError):
                    raise ValueError(f"{field['label']} must be valid JSON") from None
            _check_json_size(value, 16000)
        result[key] = value
    _check_json_size(result, 64000)
    return result


def validate_schema(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    valid = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
             "string": lambda v: isinstance(v, str), "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
             "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
             "boolean": lambda v: isinstance(v, bool), "null": lambda v: v is None}
    if isinstance(expected, list):
        matched = next((kind for kind in expected if kind in valid and valid[kind](value)), None)
        if matched is None:
            raise ValueError(f"{path} has the wrong type")
        expected = matched
    elif expected in valid and not valid[expected](value):
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    if expected == "object":
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(f"{path} contains unknown fields")
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_schema(item, schema["additionalProperties"], f"{path}.{key}")
    elif expected == "array" and "items" in schema:
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, (str, list)):
        if "minLength" in schema and len(value) < schema["minLength"] or "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{path} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"] or "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path} is too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} must be finite")
        if "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} is outside its allowed range")
    _check_json_size(value, 64000)


def approval_dialog(tool_name: str, description: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return normalize_dialog({"title": f"Approve {tool_name}"[:120], "message": (description or "Review and edit the arguments before running this tool.")[:1200],
                             "submit_label": "Approve and run", "fields": [{
                                 "id": "arguments", "label": "Tool arguments (JSON)", "type": "json", "required": True, "default": arguments,
                             }]})


def _text(value: Any, name: str, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be text up to {limit} characters")
    return value.strip() if not allow_empty else value


def _scalar(value: Any) -> Any:
    if not isinstance(value, (str, int, float, bool)) or len(str(value)) > 200 or isinstance(value, float) and not math.isfinite(value):
        raise ValueError("option values must be short scalar values")
    return value


def _check_json_size(value: Any, limit: int) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("values must be finite JSON data") from None
    if len(encoded) > limit:
        raise ValueError("interaction data is too large")
