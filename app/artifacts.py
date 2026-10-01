from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any

MAX_ARTIFACTS = 5
MAX_ROWS = 500
MAX_COLUMNS = 30
MAX_POINTS = 1000
MAX_HTML = 50000
MAX_SERIALIZED = 100000
KINDS = {"table", "bar", "line", "pie", "scatter", "metrics", "html"}


class ArtifactError(ValueError):
    pass


def validate_artifact(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("type") not in KINDS:
        raise ArtifactError("Tipo de artefacto inválido.")
    spec = dict(value)
    spec.setdefault("schema_version", 1)
    spec.setdefault("id", str(uuid.uuid4()))
    if spec["schema_version"] != 1 or not isinstance(spec.get("title"), str) or not spec["title"].strip():
        raise ArtifactError("El artefacto requiere versión 1 y título.")
    kind = spec["type"]
    data = spec.get("data")
    if kind == "html":
        if not isinstance(data, dict) or not isinstance(data.get("html"), str) or len(data["html"]) > MAX_HTML:
            raise ArtifactError("HTML inválido o demasiado grande.")
        # Scripts are disabled by the sandbox and removed for defense in depth.
        data["html"] = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", "", data["html"])
        spec["data"] = data
    elif kind == "table":
        if not isinstance(data, dict) or not isinstance(data.get("columns"), list) or not isinstance(data.get("rows"), list):
            raise ArtifactError("La tabla requiere columns y rows.")
        if len(data["columns"]) > MAX_COLUMNS or len(data["rows"]) > MAX_ROWS:
            raise ArtifactError("La tabla supera los límites permitidos.")
        if any(not isinstance(row, (list, dict)) for row in data["rows"]):
            raise ArtifactError("Fila de tabla inválida.")
    elif kind == "metrics":
        if not isinstance(data, list) or len(data) > MAX_COLUMNS or any(not isinstance(item, dict) or "label" not in item or "value" not in item for item in data):
            raise ArtifactError("Métricas inválidas.")
    else:
        if not isinstance(data, dict):
            raise ArtifactError("Dataset de gráfico inválido.")
        if kind == "scatter":
            points = data.get("points", [])
            if not isinstance(points, list) or len(points) > MAX_POINTS or any(not isinstance(p, dict) or not all(isinstance(p.get(k), (int, float)) and math.isfinite(p[k]) for k in ("x", "y")) for p in points):
                raise ArtifactError("Puntos scatter inválidos o fuera de límite.")
        else:
            labels, series = data.get("labels"), data.get("series")
            if not isinstance(labels, list) or len(labels) > MAX_POINTS or not isinstance(series, list) or not series:
                raise ArtifactError("El gráfico requiere labels y series.")
            if any(not isinstance(s, dict) for s in series):
                raise ArtifactError("Serie inválida.")
            if kind == "pie" and (len(series) != 1 or len(series[0].get("values", [])) != len(labels)):
                raise ArtifactError("El gráfico pie admite una serie compatible.")
            if any(not isinstance(s.get("values"), list) or len(s["values"]) != len(labels) or any(v is not None and (not isinstance(v, (int, float)) or not math.isfinite(v)) for v in s["values"]) or kind == "pie" and any(v is None for v in s["values"]) for s in series):
                raise ArtifactError("Serie numérica inválida.")
    raw = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > MAX_SERIALIZED:
        raise ArtifactError("El artefacto supera el tamaño permitido.")
    return spec


def sandbox_document(fragment: str) -> str:
    safe = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", "", fragment)
    return '<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'; form-action \'none\'; base-uri \'none\';">' + safe
