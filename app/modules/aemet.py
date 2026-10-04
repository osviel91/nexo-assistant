from __future__ import annotations

from collections.abc import Callable
import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.kernel import ModuleContext, ToolDefinition, ToolExecutionContext
from app.tool_results import aemet_projector


BASE_URL = "https://opendata.aemet.es/opendata"
MAX_RESPONSE_BYTES = 2_000_000


def compact_data(value: Any, budget: int = MAX_RESPONSE_BYTES) -> Any:
    """Keep useful structured values while excluding maps/base64 that exhaust tool context."""
    if isinstance(value, dict):
        result = {}
        remaining = budget - 2
        for key, item in value.items():
            if isinstance(item, str) and len(item) > 256 and any(token in key.lower() for token in ("map", "imagen", "image", "base64", "graf")):
                continue
            key_size = len(json.dumps(str(key), ensure_ascii=False)) + 1
            if remaining <= key_size + 8:
                break
            compacted = compact_data(item, remaining - key_size)
            encoded_size = len(json.dumps(compacted, ensure_ascii=False, separators=(",", ":")))
            if encoded_size + key_size > remaining:
                break
            result[key] = compacted
            remaining -= encoded_size + key_size + 1
        return result
    if isinstance(value, list):
        result = []
        remaining = budget - 2
        for item in value:
            compacted = compact_data(item, remaining)
            encoded_size = len(json.dumps(compacted, ensure_ascii=False, separators=(",", ":")))
            if encoded_size > remaining:
                break
            result.append(compacted)
            remaining -= encoded_size + 1
        if len(result) < len(value) and remaining > 40:
            result.append({"truncated": f"{len(value) - len(result)} elementos omitidos por límite de tamaño"})
        return result
    if isinstance(value, str):
        if value.startswith("data:") or (len(value) > 1000 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value)):
            return "[contenido binario omitido]"
        return value[:max(0, budget - 32)] + "…[texto recortado]" if len(value) > budget else value
    return value


def filter_records(value: Any, query: str) -> Any:
    needle = query.casefold()

    def contains(item: Any) -> bool:
        if isinstance(item, str):
            return needle in item.casefold()
        if isinstance(item, dict):
            return any(contains(child) for child in item.values())
        if isinstance(item, list):
            return any(contains(child) for child in item)
        return False

    if isinstance(value, list):
        return [item for item in value if contains(item)]
    if isinstance(value, dict):
        return {key: filter_records(item, query) if isinstance(item, list) else item for key, item in value.items()}
    return value


def register_aemet_tool(context: ModuleContext, api_key: Callable[[], str | None], client_factory: Callable[..., httpx.AsyncClient] | None = None) -> None:
    make_client = client_factory or httpx.AsyncClient

    async def consult(_execution: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        params = arguments.get("params", {})
        filter_text = arguments.get("filter")
        if not isinstance(path, str) or not path.startswith("/api/") or ".." in path or "?" in path or "#" in path:
            return {"error": {"code": "invalid_path", "message": "Usa una ruta /api/... del catálogo de AEMET OpenData."}}
        if not isinstance(params, dict) or len(params) > 20 or any(
            not isinstance(key, str) or key.lower() == "api_key" or not isinstance(value, (str, int, float))
            for key, value in params.items()
        ):
            return {"error": {"code": "invalid_params", "message": "Los parámetros deben ser valores simples; api_key no está permitido."}}
        if filter_text is not None and (not isinstance(filter_text, str) or len(filter_text) > 100):
            return {"error": {"code": "invalid_filter", "message": "El filtro debe tener como máximo 100 caracteres."}}
        token = api_key()
        if not token:
            return {"error": {"code": "not_configured", "message": "Configura el token de AEMET en Settings > Tools."}}

        try:
            async with make_client(timeout=20, follow_redirects=False) as client:
                response = await client.get(BASE_URL + path, params={**params, "api_key": token})
                if response.status_code == 429:
                    return {"error": {"code": "rate_limited", "message": "AEMET ha limitado las consultas. No reintentes automáticamente; informa al usuario y espera antes de volver a consultar."}}
                if response.status_code >= 400:
                    return {"error": {"code": "aemet_http_error", "message": f"AEMET respondió HTTP {response.status_code}."}}
                if len(response.content) > MAX_RESPONSE_BYTES:
                    return {"error": {"code": "response_too_large", "message": "La respuesta de AEMET supera el límite permitido."}}
                try:
                    payload = response.json()
                except ValueError:
                    return {"error": {"code": "invalid_json", "message": "AEMET devolvió una respuesta no válida."}}

                if not isinstance(payload, dict) or not isinstance(payload.get("datos"), str):
                    return {"data": payload}
                data_url = urlparse(payload["datos"])
                if (data_url.scheme != "https" or data_url.hostname not in {"opendata.aemet.es", "datos.aemet.es"}
                        or data_url.port not in {None, 443} or data_url.username or data_url.password):
                    return {"error": {"code": "invalid_data_url", "message": "AEMET devolvió una URL de datos no permitida."}}
                data_response = await client.get(payload["datos"])
                if data_response.status_code == 429:
                    return {"error": {"code": "rate_limited", "message": "AEMET ha limitado las consultas. No reintentes automáticamente; informa al usuario y espera antes de volver a consultar."}}
                if data_response.status_code >= 400:
                    return {"error": {"code": "aemet_data_error", "message": f"La descarga de datos respondió HTTP {data_response.status_code}."}}
                if len(data_response.content) > MAX_RESPONSE_BYTES:
                    return {"error": {"code": "response_too_large", "message": "Los datos de AEMET superan el límite permitido."}}
                try:
                    data = data_response.json()
                except ValueError:
                    data = data_response.text
                if filter_text:
                    data = filter_records(data, filter_text)
                return {"data": compact_data(data), "metadata": {key: value for key, value in payload.items() if key != "datos"}}
        except httpx.TimeoutException:
            return {"error": {"code": "timeout", "message": "AEMET agotó el tiempo de espera."}}
        except httpx.RequestError:
            return {"error": {"code": "connection_error", "message": "No se pudo conectar con AEMET."}}

    context.tools.register(ToolDefinition(
        "aemet.opendata",
        "Consulta servicios GET de AEMET OpenData. Usa la ruta exacta del catálogo empezando por /api/; para pronóstico municipal, consulta /api/maestro/municipios con filter igual al nombre de la localidad para localizar el identificador AEMET (no uses el código postal ni inventes IDs), y úsalo en /api/prediccion/especifica/municipio/diaria/{idema}. También puedes consultar observación, climatología, avisos, predicción horaria, playas, montaña y servicios del catálogo. Sustituye variables de ruta y envía parámetros simples en params. Descarga y compacta datos AEMET omitiendo mapas/base64 para evitar truncado. Si devuelve rate_limited, no reintentes automáticamente. Requiere token en Settings > Tools.",
        {"type": "object", "properties": {"path": {"type": "string", "minLength": 6, "maxLength": 1000}, "params": {"type": "object", "additionalProperties": {"type": ["string", "number"]}}, "filter": {"type": "string", "maxLength": 100, "description": "Filtra localmente los registros devueltos, útil para buscar municipios por nombre en /api/maestro/municipios."}}, "required": ["path"], "additionalProperties": False},
        consult,
        "native",
        "aemet",
        action="read_only",
        result_projector=aemet_projector,
    ))
