from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from app.kernel import InterfaceExtension, ModuleContext, ModuleManifest, ToolDefinition, ToolExecutionContext

logger = logging.getLogger("nexo.mcp")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class MCPProtocolError(Exception):
    pass


class MCPModule:
    manifest = ModuleManifest(
        id="mcp",
        name="Model Context Protocol tools",
        version="1.0.0",
        api_version=1,
        capabilities=("mcp", "tools"),
    )
    interface_extensions = ()

    def __init__(self, client_factory: Callable[[str], Any] | None = None) -> None:
        self.client_factory = client_factory or self._sdk_client
        self.servers: list[dict[str, Any]] = []
        self.clients: dict[str, Any] = {}
        self._contexts: dict[str, Any] = {}

    def register(self, context: ModuleContext) -> None:
        self.servers = parse_server_config(context.settings.get("mcp_servers", "[]"))

    def startup(self, context: ModuleContext) -> None:
        try:
            asyncio.run(self.startup_async(context))
        except RuntimeError:
            self._diagnose_all("mcp_connection_error")

    async def startup_async(self, context: ModuleContext) -> None:
        for server in self.servers:
            if not server["enabled"]:
                continue
            try:
                await self._connect_server(context, server)
            except Exception as exc:
                client = self.clients.pop(server["id"], None)
                if client is not None:
                    try:
                        await client.__aexit__(None, None, None)
                    except Exception:
                        logger.exception("MCP cleanup failed", extra={"server_id": server["id"]})
                self._fail(server, "mcp_protocol_error" if isinstance(exc, MCPProtocolError) else "mcp_connection_error")
                logger.exception("MCP server startup failed", extra={"server_id": server["id"], "error_type": type(exc).__name__})

    async def shutdown_async(self, context: ModuleContext) -> None:
        for server_id, client in list(self.clients.items()):
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                logger.exception("MCP server shutdown failed", extra={"server_id": server_id})
        self.clients.clear()
        self._contexts.clear()

    def shutdown(self, context: ModuleContext) -> None:
        try:
            asyncio.run(self.shutdown_async(context))
        except RuntimeError:
            logger.error("MCP shutdown skipped because an event loop is active")

    def run_hook(self, hook: str, context: ModuleContext, payload: object) -> None:
        return None

    def catalog_status(self) -> dict[str, Any]:
        return {
            "servers": [
                {
                    "id": server["id"],
                    "enabled": server["enabled"],
                    "connected": server["connected"],
                    "discovered_tools": server["discovered_tools"],
                    "exposed_tools": server["exposed_tools"],
                    "last_error": server["last_error"],
                }
                for server in self.servers
            ]
        }

    async def _connect_server(self, context: ModuleContext, server: dict[str, Any]) -> None:
        client = self.client_factory(server["url"])
        await asyncio.wait_for(client.__aenter__(), server["timeout"])
        self.clients[server["id"]] = client
        self._contexts[server["id"]] = context
        tools_result = await asyncio.wait_for(client.list_tools(), server["timeout"])
        raw_tools = getattr(tools_result, "tools", None)
        if raw_tools is None and isinstance(tools_result, dict):
            raw_tools = tools_result.get("tools")
        if not isinstance(raw_tools, list):
            raise MCPProtocolError("tools/list returned an invalid result")
        server["connected"] = True
        for remote_tool in raw_tools:
            self._register_tool(context, server, remote_tool)

    def _register_tool(self, context: ModuleContext, server: dict[str, Any], remote_tool: Any) -> None:
        name = getattr(remote_tool, "name", None)
        description = getattr(remote_tool, "description", "")
        schema = getattr(remote_tool, "inputSchema", None)
        if isinstance(remote_tool, dict):
            name = remote_tool.get("name")
            description = remote_tool.get("description", "")
            schema = remote_tool.get("inputSchema", remote_tool.get("input_schema"))
        if not isinstance(name, str) or not name or not isinstance(description, str) or not isinstance(schema, dict):
            self._diagnose(server, "mcp_protocol_error")
            return
        if schema.get("type") != "object":
            self._diagnose(server, "mcp_protocol_error")
            return
        server["discovered_tools"] += 1
        if name not in server["allowed_tools"]:
            return
        exposed_name = namespace(server["id"], name)
        if context.tools.has(exposed_name):
            self._diagnose(server, "tool_name_collision")
            return

        async def handler(execution: ToolExecutionContext, arguments: dict[str, Any]) -> dict[str, Any]:
            return await self._call(server, name, arguments)

        context.tools.register(ToolDefinition(exposed_name, description[:1000], schema, handler, "mcp", self.manifest.id))
        server["exposed_tools"] += 1

    async def _call(self, server: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self.clients.get(server["id"])
        if client is None or name not in server["allowed_tools"]:
            return {"error": {"code": "tool_not_allowed", "message": "La herramienta MCP no está permitida."}}
        try:
            result = await asyncio.wait_for(client.call_tool(name, arguments), server["timeout"])
        except asyncio.TimeoutError:
            return {"error": {"code": "tool_timeout", "message": "La herramienta MCP agotó el tiempo de espera."}}
        except Exception:
            logger.exception("MCP tool call failed", extra={"server_id": server["id"], "tool": name})
            return {"error": {"code": "tool_execution_error", "message": "La herramienta MCP no pudo completar la operación."}}
        if getattr(result, "is_error", False):
            return {"error": {"code": "tool_execution_error", "message": "El servidor MCP rechazó la operación."}}
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        content = getattr(result, "content", None)
        texts = [getattr(item, "text", "") for item in content or []]
        return {"content": "\n".join(text for text in texts if isinstance(text, str))}

    def _fail(self, server: dict[str, Any], code: str) -> None:
        server["last_error"] = code

    def _diagnose(self, server: dict[str, Any], code: str) -> None:
        server["last_error"] = code
        logger.error("MCP diagnostic", extra={"server_id": server["id"], "code": code})

    def _diagnose_all(self, code: str) -> None:
        for server in self.servers:
            if server["enabled"]:
                self._diagnose(server, code)

    @staticmethod
    def _sdk_client(url: str) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        return _SDKClient(url, ClientSession, streamablehttp_client)


class _SDKClient:
    def __init__(self, url: str, session_type: Any, transport: Any) -> None:
        self.url = url
        self.session_type = session_type
        self.transport = transport
        self._transport_context = None
        self._session = None

    async def __aenter__(self) -> "_SDKClient":
        self._transport_context = self.transport(self.url)
        read_stream, write_stream, _ = await self._transport_context.__aenter__()
        self._session = self.session_type(read_stream, write_stream)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session is not None:
            await self._session.__aexit__(*args)
        if self._transport_context is not None:
            await self._transport_context.__aexit__(*args)

    async def list_tools(self) -> Any:
        return await self._session.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._session.call_tool(name, arguments)


def namespace(server_id: str, tool_name: str) -> str:
    safe_server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)
    safe_tool = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)
    return f"mcp__{safe_server}__{safe_tool}"


def parse_server_config(raw: Any) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    servers = []
    for item in value:
        if not isinstance(item, dict):
            continue
        server_id, url = item.get("id"), item.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        allowed = item.get("allowed_tools", [])
        if not isinstance(server_id, str) or not _SAFE_NAME.fullmatch(server_id):
            continue
        if not parsed or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if not isinstance(allowed, list) or not all(isinstance(tool, str) for tool in allowed):
            continue
        try:
            timeout = max(float(item.get("timeout", 15)), 0.1)
        except (TypeError, ValueError):
            continue
        servers.append({
            "id": server_id,
            "url": url,
            "enabled": item.get("enabled", True) is True,
            "allowed_tools": set(allowed),
            "timeout": timeout,
            "connected": False,
            "discovered_tools": 0,
            "exposed_tools": 0,
            "last_error": None,
        })
    return servers
