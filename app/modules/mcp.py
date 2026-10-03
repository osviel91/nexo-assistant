from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from app.kernel import ModuleContext, ModuleManifest, ToolDefinition, ToolExecutionContext

class MCPProtocolError(Exception):
    pass


def validate_server(name: str, slug: str, transport: str, endpoint: str, timeout: float) -> None:
    parsed = urlparse(endpoint)
    if not name.strip() or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?", slug):
        raise ValueError("Invalid server name or slug")
    if transport != "streamable-http" or parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a Streamable HTTP endpoint without URL credentials, query, or fragment")
    if not 0.1 <= timeout <= 120:
        raise ValueError("Timeout must be between 0.1 and 120 seconds")


def normalized_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise MCPProtocolError("Invalid input schema")
    try:
        safe = json.loads(json.dumps(schema))
    except (TypeError, ValueError):
        raise MCPProtocolError("Invalid input schema") from None
    if len(json.dumps(safe)) > 16000:
        raise MCPProtocolError("Input schema too large")
    return safe


class MCPManager:
    """MCP transport and discovery boundary; persisted metadata is supplied by callbacks."""

    manifest = ModuleManifest("mcp", "Model Context Protocol", "2.0.0", 1, ("mcp", "tools"))
    interface_extensions = ()

    def __init__(self, repository: Any, client_factory: Callable[[str, str | None], Any] | None = None) -> None:
        self.repository = repository
        self.client_factory = client_factory or self._sdk_client
        self.context: ModuleContext | None = None

    def register(self, context: ModuleContext) -> None:
        self.context = context
        context.services["mcp"] = self

    def startup(self, context: ModuleContext) -> None:
        self._sync_tools()

    def shutdown(self, context: ModuleContext) -> None:
        for tool in self.repository.tools():
            context.tools.unregister(tool["id"])

    def run_hook(self, hook: str, context: ModuleContext, payload: object) -> None:
        return None

    def _sync_tools(self) -> None:
        if not self.context:
            return
        for tool in self.repository.tools():
            self.context.tools.unregister(tool["id"])
        for tool in self.repository.tools(enabled=True):
            server = self.repository.server(tool["server_id"])
            if not server or not server["enabled"] or server["status"] != "connected" or self.context.tools.has(tool["id"]):
                continue
            async def invoke(execution: ToolExecutionContext, arguments: dict[str, Any], tool=tool, server=server):
                return await self.call(server, tool, arguments)
            self.context.tools.register(ToolDefinition(tool["id"], tool["description"], json.loads(tool["input_schema"]), invoke, "mcp", "mcp", action=tool.get("action")))

    async def refresh(self, server: dict[str, Any]) -> dict[str, Any]:
        self.repository.status(server["id"], "connecting", None)
        try:
            async with self.client_factory(server["endpoint"], self.repository.auth_token(server["id"])) as client:
                await asyncio.wait_for(client.initialize(), server["timeout"])
                result = await asyncio.wait_for(client.list_tools(), server["timeout"])
            raw_tools = getattr(result, "tools", None)
            if raw_tools is None and isinstance(result, dict):
                raw_tools = result.get("tools")
            if not isinstance(raw_tools, list):
                raise MCPProtocolError("Invalid tools/list result")
            tools = []
            for item in raw_tools:
                get = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
                name = get("name")
                if not isinstance(name, str) or not name or len(name) > 128:
                    continue
                schema = normalized_schema(get("inputSchema", get("input_schema")))
                identity = f"mcp.{server['slug']}.{name}"
                annotations = get("annotations", {})
                annotation = annotations.get if isinstance(annotations, dict) else lambda key, default=None: getattr(annotations, key, default)
                action = "read_only" if annotation("readOnlyHint", annotation("read_only_hint")) is True else "unknown"
                tools.append({"id": identity, "remote_name": name, "description": str(get("description", ""))[:1000], "input_schema": json.dumps(schema), "action": action})
            self.repository.replace_tools(server["id"], tools)
            self.repository.status(server["id"], "connected", None)
            self._sync_tools()
            return self.repository.server(server["id"])
        except Exception as error:
            category = "authentication_failed" if getattr(error, "status_code", None) in (401, 403) else "discovery_failed" if isinstance(error, MCPProtocolError) else "unreachable"
            self.repository.status(server["id"], category, category)
            self._sync_tools()
            raise

    async def call(self, server: dict[str, Any], tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        status, truncated = "completed", False
        try:
            current_server = self.repository.server(server["id"])
            if not current_server or not current_server["enabled"] or current_server["status"] != "connected":
                status = "stale_tool"
                return {"error": {"code": "stale_tool", "message": "MCP tool is no longer available."}}
            schema = json.loads(tool["input_schema"])
            properties = schema.get("properties", {})
            if any(key not in properties for key in arguments) or any(key not in arguments for key in schema.get("required", [])):
                return {"error": {"code": "invalid_arguments", "message": "Arguments do not match the MCP tool schema."}}
            async def invoke():
                async with self.client_factory(server["endpoint"], self.repository.auth_token(server["id"])) as client:
                    await client.initialize()
                    return await client.call_tool(tool["remote_name"], arguments)

            result = await asyncio.wait_for(invoke(), server["timeout"])
            if getattr(result, "is_error", False):
                status = "mcp_protocol_error"
                return {"error": {"code": "mcp_protocol_error", "message": "MCP tool returned an error."}}
            raw_content = getattr(result, "content", None)
            if not isinstance(raw_content, list):
                raise MCPProtocolError("Malformed MCP result")
            content = []
            for item in raw_content:
                if getattr(item, "type", "") == "text" and isinstance(getattr(item, "text", None), str):
                    content.append(item.text)
            structured = getattr(result, "structured_content", None)
            payload = {"content": "\n".join(content)}
            if structured is not None:
                payload["structured_data"] = json.loads(json.dumps(structured, ensure_ascii=False))
            return payload
        except asyncio.TimeoutError:
            status = "invocation_timeout"
            self.repository.status(server["id"], "unreachable", status)
            self._sync_tools()
            return {"error": {"code": "invocation_timeout", "message": "MCP tool timed out."}}
        except MCPProtocolError:
            status = "malformed_result"
            self.repository.status(server["id"], "degraded", status)
            self._sync_tools()
            return {"error": {"code": "malformed_result", "message": "MCP server returned a malformed result."}}
        except Exception:
            status = "mcp_unavailable"
            self.repository.status(server["id"], "unreachable", status)
            self._sync_tools()
            return {"error": {"code": "mcp_unavailable", "message": "MCP server is unavailable."}}
        finally:
            self.repository.invocation(server["id"], tool["id"], round((time.monotonic() - started) * 1000, 2), status, truncated)

    def catalog_status(self) -> dict[str, Any]:
        return {"servers": self.repository.servers()}

    @staticmethod
    def _sdk_client(endpoint: str, bearer_token: str | None = None) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        return _SDKClient(endpoint, ClientSession, streamablehttp_client, headers)


class _SDKClient:
    def __init__(self, url: str, session_type: Any, transport: Any, headers: dict[str, str] | None = None) -> None:
        self.url, self.session_type, self.transport, self.headers = url, session_type, transport, headers

    async def __aenter__(self):
        self.transport_context = self.transport(self.url, headers=self.headers)
        read, write, _ = await self.transport_context.__aenter__()
        self.session = self.session_type(read, write)
        await self.session.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.session.__aexit__(*args)
        await self.transport_context.__aexit__(*args)

    async def initialize(self):
        return await self.session.initialize()

    async def list_tools(self):
        return await self.session.list_tools()

    async def call_tool(self, name, arguments):
        return await self.session.call_tool(name, arguments)
