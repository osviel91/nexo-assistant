from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from app.kernel import ModuleContext, ToolDefinition, ToolExecutionContext


BASE_URL = "https://opendata.aemet.es/opendata"
MAX_RESPONSE_BYTES = 2_000_000


def register_aemet_tool(context: ModuleContext, api_key: Callable[[], str | None], client_factory: Callable[..., httpx.AsyncClient] | None = None) -> None:
    make_client = client_factory or httpx.AsyncClient

    async def consult(_execution: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        params = arguments.get("params", {})
        if not isinstance(path, str) or not path.startswith("/api/") or ".." in path or "?" in path or "#" in path:
            return {"error": {"code": "invalid_path", "message": "Usa una ruta /api/... del catálogo de AEMET OpenData."}}
        if not isinstance(params, dict) or len(params) > 20 or any(
            not isinstance(key, str) or key.lower() == "api_key" or not isinstance(value, (str, int, float))
            for key, value in params.items()
        ):
            return {"error": {"code": "invalid_params", "message": "Los parámetros deben ser valores simples; api_key no está permitido."}}
        token = api_key()
        if not token:
            return {"error": {"code": "not_configured", "message": "Configura el token de AEMET en Settings > Tools."}}

        try:
            async with make_client(timeout=20, follow_redirects=False) as client:
                response = await client.get(BASE_URL + path, params={**params, "api_key": token})
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
                if data_response.status_code >= 400:
                    return {"error": {"code": "aemet_data_error", "message": f"La descarga de datos respondió HTTP {data_response.status_code}."}}
                if len(data_response.content) > MAX_RESPONSE_BYTES:
                    return {"error": {"code": "response_too_large", "message": "Los datos de AEMET superan el límite permitido."}}
                try:
                    data = data_response.json()
                except ValueError:
                    data = data_response.text
                return {"data": data, "metadata": {key: value for key, value in payload.items() if key != "datos"}}
        except httpx.TimeoutException:
            return {"error": {"code": "timeout", "message": "AEMET agotó el tiempo de espera."}}
        except httpx.RequestError:
            return {"error": {"code": "connection_error", "message": "No se pudo conectar con AEMET."}}

    context.tools.register(ToolDefinition(
        "aemet.opendata",
        "Consulta cualquier servicio GET de AEMET OpenData. Usa path con la ruta exacta del catálogo, empezando por /api/ (por ejemplo /api/prediccion/especifica/municipio/diaria/{idema}, /api/observacion/convencional/todas o /api/valores/climatologicos/diarios/datos/estacion/{idema}/fechaIni/{fechaIniStr}/fechaFin/{fechaFinStr}); sustituye las variables en la ruta y pasa solo parámetros simples en params. AEMET devuelve los datos mediante su campo datos; esta herramienta los descarga y devuelve también los metadatos. Requiere token configurado en Settings > Tools.",
        {"type": "object", "properties": {"path": {"type": "string", "minLength": 6, "maxLength": 1000}, "params": {"type": "object", "additionalProperties": {"type": ["string", "number"]}}}, "required": ["path"], "additionalProperties": False},
        consult,
        "native",
        "aemet",
    ))
