from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from app.kernel import InterfaceExtension, ModuleContext, ModuleManifest, ToolDefinition, ToolExecutionContext


class SearXNGSearchError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class WebSearchSearxngModule:
    manifest = ModuleManifest(
        id="web-search-searxng",
        name="SearXNG web search",
        version="1.0.0",
        api_version=1,
        capabilities=("web-search", "tool-calling"),
    )
    interface_extensions = (
        InterfaceExtension(id="web-search-status", kind="chat-status", label="Búsqueda web"),
    )

    def __init__(self, client_factory: Callable[..., httpx.AsyncClient] | None = None) -> None:
        self.client_factory = client_factory or httpx.AsyncClient

    def register(self, context: ModuleContext) -> None:
        base_url = str(context.settings.get("searxng_url", "")).strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("SearXNG URL must be an absolute HTTP(S) URL")
        language = str(context.settings.get("searxng_language", "all")) or "all"
        safesearch = min(max(int(context.settings.get("searxng_safesearch", 1)), 0), 2)
        max_results = min(max(int(context.settings.get("searxng_max_results", 5)), 1), 10)
        timeout = max(float(context.settings.get("searxng_timeout", 10)), 0.1)

        async def search(_execution: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > 500:
                return {"error": {"code": "invalid_query", "message": "La consulta debe tener entre 1 y 500 caracteres."}}
            params = {"q": query.strip(), "format": "json", "language": language, "safesearch": safesearch}
            try:
                async with self.client_factory(timeout=timeout, follow_redirects=False) as client:
                    response = await client.get(base_url + "/search", params=params)
                if response.status_code >= 400:
                    raise SearXNGSearchError("http_error", f"SearXNG respondió HTTP {response.status_code}.")
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                    raise SearXNGSearchError("invalid_response", "SearXNG devolvió una respuesta inválida.")
            except httpx.TimeoutException:
                raise SearXNGSearchError("timeout", "La búsqueda web agotó el tiempo de espera.")
            except httpx.RequestError:
                raise SearXNGSearchError("connection_error", "No se pudo conectar con el servicio de búsqueda.")
            except ValueError:
                raise SearXNGSearchError("invalid_json", "SearXNG devolvió JSON inválido.")

            results = []
            for item in payload["results"]:
                if not isinstance(item, dict):
                    continue
                title, url = item.get("title"), item.get("url")
                if not isinstance(title, str) or not isinstance(url, str):
                    continue
                if urlparse(url).scheme not in {"http", "https"}:
                    continue
                snippet = item.get("content", "")
                results.append({"title": title[:240], "url": url[:2000], "snippet": snippet[:600] if isinstance(snippet, str) else ""})
                if len(results) >= max_results:
                    break
            return {"results": results}

        async def safe_search(execution: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                return await search(execution, arguments)
            except SearXNGSearchError as exc:
                return {"error": {"code": exc.code, "message": str(exc)}}

        context.tools.register(ToolDefinition(
            name="web_search",
            description="Busca en la web. Usa los resultados relevantes y cita las fuentes como [1], [2], etc.",
            parameters={"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500}}, "required": ["query"], "additionalProperties": False},
            handler=safe_search,
        ))

    def startup(self, context: ModuleContext) -> None:
        return None

    def shutdown(self, context: ModuleContext) -> None:
        return None

    def run_hook(self, hook: str, context: ModuleContext, payload: object) -> None:
        return None
