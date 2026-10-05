from __future__ import annotations

from collections.abc import Callable
import json
import logging
import re
import unicodedata
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from app.kernel import ModuleContext, ToolDefinition, ToolExecutionContext
from app.tool_results import aemet_projector
from app.diagnostics import diagnostic


BASE_URL = "https://opendata.aemet.es/opendata"
MAX_RESPONSE_BYTES = 2_000_000
logger = logging.getLogger(__name__)


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
    municipality_cache: list[dict[str, str]] | None = None

    async def get_data(client: httpx.AsyncClient, path: str, params: dict[str, Any], token: str) -> tuple[Any, dict[str, Any]]:
        response = await client.get(BASE_URL + path, params={**params, "api_key": token})
        if response.status_code >= 400:
            raise AemetFailure("upstream_http_error", response.status_code)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise AemetFailure("upstream_response_too_large")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AemetFailure("upstream_invalid_data", response.status_code) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("datos"), str):
            return payload, {}
        data_url = urlparse(payload["datos"])
        if (data_url.scheme != "https" or data_url.hostname not in {"opendata.aemet.es", "datos.aemet.es"}
                or data_url.port not in {None, 443} or data_url.username or data_url.password):
            raise AemetFailure("upstream_invalid_data", response.status_code)
        data_response = await client.get(payload["datos"])
        if data_response.status_code >= 400:
            raise AemetFailure("upstream_http_error", data_response.status_code)
        if len(data_response.content) > MAX_RESPONSE_BYTES:
            raise AemetFailure("upstream_response_too_large", data_response.status_code)
        try:
            return data_response.json(), {key: value for key, value in payload.items() if key != "datos"}
        except ValueError as exc:
            raise AemetFailure("upstream_invalid_data", data_response.status_code) from exc

    class AemetFailure(Exception):
        def __init__(self, category: str, status: int | None = None):
            self.category, self.status = category, status

    def normalize_name(value: str) -> str:
        return " ".join("".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                                if not unicodedata.combining(char)).split())

    async def resolve_municipality(client: httpx.AsyncClient, name: str, token: str) -> tuple[dict[str, str] | None, list[dict[str, str]], int]:
        nonlocal municipality_cache
        requests = 0
        if municipality_cache is None:
            records, _ = await get_data(client, "/api/maestro/municipios", {}, token)
            requests = 2
            municipality_cache = [{"name": str(item.get("nombre", "")), "municipality_code": str(item.get("id", ""))}
                                  for item in records if isinstance(item, dict) and item.get("nombre") and item.get("id")]
        matches = [item for item in municipality_cache if normalize_name(item["name"]) == normalize_name(name)]
        return (matches[0], [], requests) if len(matches) == 1 else (None, matches[:5], requests)

    async def consult(_execution: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
        operation = arguments.get("operation", "raw")
        if operation == "forecast_daily":
            location = arguments.get("location")
            code = arguments.get("municipality_code")
            requested_date = arguments.get("date")
            if not isinstance(requested_date, str):
                period = arguments.get("period", "tomorrow")
                offsets = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}
                if period not in offsets:
                    return {"error": {"code": "invalid_arguments", "message": "period debe ser today, tomorrow o day_after_tomorrow; o indica date (YYYY-MM-DD)."}}
                requested_date = (date.today() + timedelta(days=offsets[period])).isoformat()
            try:
                date.fromisoformat(requested_date)
            except (ValueError, TypeError):
                return {"error": {"code": "invalid_arguments", "message": "Indica location o municipality_code y date (YYYY-MM-DD) o period: today, tomorrow o day_after_tomorrow."}}
            if not (isinstance(code, str) and re.fullmatch(r"\d{5}", code)) and not (isinstance(location, str) and location.strip()):
                return {"error": {"code": "invalid_arguments", "message": "Se requiere location o municipality_code para forecast_daily."}}
            token = api_key()
            if not token:
                return {"error": {"code": "not_configured", "message": "Configura el token de AEMET en Settings > Tools."}}
            requests = 0
            resolution = "explicit_code" if code else "name"
            try:
                async with make_client(timeout=20, follow_redirects=False) as client:
                    if not code:
                        identity, candidates, catalogue_requests = await resolve_municipality(client, location, token)
                        requests += catalogue_requests
                        if identity is None:
                            category = "location_ambiguous" if candidates else "location_not_found"
                            return {"error": {"code": category, "candidates": candidates, "message": "Indica un municipio inequívoco." if candidates else "No se encontró el municipio."}}
                        code, location = identity["municipality_code"], identity["name"]
                    data, _ = await get_data(client, f"/api/prediccion/especifica/municipio/diaria/{code}", {}, token)
                    requests += 2
                    forecast = data if isinstance(data, list) else data.get("prediccion", {}).get("dia", []) if isinstance(data, dict) else []
                    forecast = [row for row in forecast if isinstance(row, dict) and row.get("fecha") == requested_date]
                    diagnostic(logger, "aemet_semantic", operation=operation, location_resolution=resolution,
                               internal_upstream_requests=requests, upstream_category="success" if forecast else "forecast_not_available",
                               projection_strategy="aemet", original_size=len(json.dumps(data, ensure_ascii=False)))
                    if not forecast:
                        return {"error": {"code": "forecast_not_available", "message": "AEMET no dispone de pronóstico para esa fecha."}}
                    return {"structured_data": {"location": {"name": location or str(code), "municipality_code": code}, "forecast": forecast}}
            except AemetFailure as error:
                diagnostic(logger, "aemet_semantic", operation=operation, location_resolution=resolution,
                           internal_upstream_requests=requests, upstream_status=error.status,
                           upstream_category=error.category, projection_strategy="aemet")
                result = {"code": error.category, "message": "AEMET no pudo completar la consulta."}
                if error.status is not None:
                    result["upstream_status"] = error.status
                return {"error": result}
            except httpx.TimeoutException:
                return {"error": {"code": "upstream_http_error", "message": "AEMET agotó el tiempo de espera."}}
            except httpx.RequestError:
                return {"error": {"code": "upstream_http_error", "message": "No se pudo conectar con AEMET."}}

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
                    return {"error": {"code": "aemet_http_error", "upstream_status": response.status_code,
                                      "upstream_category": "http_error", "message": f"AEMET respondió HTTP {response.status_code}."}}
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
                    return {"error": {"code": "aemet_data_error", "upstream_status": data_response.status_code,
                                       "upstream_category": "http_error", "message": f"La descarga de datos respondió HTTP {data_response.status_code}."}}
                if len(data_response.content) > MAX_RESPONSE_BYTES:
                    return {"error": {"code": "response_too_large", "message": "Los datos de AEMET superan el límite permitido."}}
                try:
                    data = data_response.json()
                except ValueError:
                    return {"error": {"code": "invalid_json", "upstream_status": data_response.status_code,
                                      "upstream_category": "invalid_json", "message": "AEMET devolvió datos que no son JSON válido."}}
                if filter_text:
                    data = filter_records(data, filter_text)
                return {"data": compact_data(data), "metadata": {key: value for key, value in payload.items() if key != "datos"}}
        except httpx.TimeoutException:
            return {"error": {"code": "timeout", "message": "AEMET agotó el tiempo de espera."}}
        except httpx.RequestError:
            return {"error": {"code": "connection_error", "message": "No se pudo conectar con AEMET."}}

    context.tools.register(ToolDefinition(
        "aemet.opendata",
        "Consulta el pronóstico diario por municipio (operation forecast_daily; location o municipality_code y date o period today/tomorrow/day_after_tomorrow). También permite acceso experto raw con path /api/ y params.",
        {"type": "object", "properties": {"operation": {"type": "string", "enum": ["forecast_daily", "raw"]}, "location": {"type": "string", "maxLength": 100}, "municipality_code": {"type": "string", "pattern": "^\\d{5}$"}, "date": {"type": "string", "format": "date"}, "period": {"type": "string", "enum": ["today", "tomorrow", "day_after_tomorrow"]}, "path": {"type": "string", "minLength": 6, "maxLength": 1000}, "params": {"type": "object", "additionalProperties": {"type": ["string", "number"]}}, "filter": {"type": "string", "maxLength": 100}}, "additionalProperties": False},
        consult,
        "native",
        "aemet",
        action="read_only",
        result_projector=aemet_projector,
    ))
