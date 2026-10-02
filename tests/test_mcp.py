import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.kernel import ModuleContext, ToolExecutionContext, ToolRegistry
from app.migrations import migrate
from app.modules.mcp import MCPManager, MCPProtocolError, normalized_schema, validate_server
from app.native_tools import register_native_tools
from app.agent import AgentRunRequest, AgentRuntime
from app.agent_model import ModelStreamChunk
from app.tools import ExposurePolicy, ToolExecutor


class Repo:
    def __init__(self):
        self.server_value = {"id": "s1", "slug": "demo", "endpoint": "http://localhost/mcp", "enabled": True, "timeout": .1, "status": "connected"}
        self.items = []
        self.diagnostics = []

    def server(self, _): return self.server_value
    def tools(self, enabled=None): return [t for t in self.items if enabled is None or t["enabled"] == enabled]
    def replace_tools(self, _id, tools): self.items = [dict(t, enabled=False, server_id="s1") for t in tools]
    def status(self, _id, status, error): self.server_value.update(status=status, error_category=error)
    def auth_token(self, _id): return None
    def invocation(self, server_id, tool_id, duration, status, truncated): self.diagnostics.append((server_id, tool_id, duration, status, truncated))


class FakeClient:
    def __init__(self, tools=None, result=None, error=None, delay=0):
        self.tools, self.result, self.error, self.delay = tools or [], result, error, delay
        self.initialized = False
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def initialize(self): self.initialized = True
    async def list_tools(self):
        if self.error: raise self.error
        return SimpleNamespace(tools=self.tools)
    async def call_tool(self, name, arguments):
        if self.delay: await asyncio.sleep(self.delay)
        if self.error: raise self.error
        return self.result


class MCPTests(unittest.TestCase):
    def test_migration_and_server_transport_validation(self):
        db = sqlite3.connect(":memory:")
        migrate(db)
        self.assertTrue(db.execute("SELECT 1 FROM sqlite_master WHERE name='mcp_servers'").fetchone())
        validate_server("Demo", "demo", "streamable-http", "https://example.test/mcp", 15)
        for args in [("x", "bad slug", "streamable-http", "https://x/mcp", 1), ("x", "x", "stdio", "https://x", 1), ("x", "x", "streamable-http", "https://u:p@x", 1), ("x", "x", "streamable-http", "https://x/mcp?token=secret", 1)]:
            with self.assertRaises(ValueError): validate_server(*args)

    def test_bearer_credential_is_used_but_never_returned(self):
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            old_path, old_factory = main.DB_PATH, main.mcp_manager.client_factory
            main.DB_PATH = Path(directory) / "mcp-auth.sqlite3"
            main.startup()
            seen = []
            client = FakeClient([{"name": "read", "inputSchema": {"type": "object"}}], SimpleNamespace(content=[], structured_content=None, is_error=False))
            main.mcp_manager.client_factory = lambda endpoint, token: (seen.append((endpoint, token)) or client)
            token = "test-bearer-secret"
            try:
                with TestClient(main.app) as http:
                    created = http.post("/api/mcp/servers", json={"name": "Private", "slug": "private", "endpoint": "https://mcp.example/mcp", "transport": "streamable-http", "enabled": True, "auth_type": "bearer", "auth_token": token})
                    self.assertEqual(created.status_code, 200)
                    server = created.json()
                    self.assertTrue(server["has_auth"])
                    self.assertNotIn(token, created.text)
                    connected = http.post(f"/api/mcp/servers/{server['id']}/connect")
                    self.assertEqual(connected.status_code, 200)
                    self.assertEqual(seen, [("https://mcp.example/mcp", token)])
                    self.assertNotIn(token, connected.text)
                    unchanged = http.put(f"/api/mcp/servers/{server['id']}", json={"name": "Private", "slug": "private", "endpoint": "https://mcp.example/mcp", "transport": "streamable-http", "enabled": True, "auth_type": "bearer"})
                    self.assertTrue(unchanged.json()["has_auth"])
                    removed = http.put(f"/api/mcp/servers/{server['id']}", json={"name": "Private", "slug": "private", "endpoint": "https://mcp.example/mcp", "transport": "streamable-http", "enabled": True, "auth_type": "none"})
                    self.assertFalse(removed.json()["has_auth"])
                    self.assertNotIn(token, removed.text)
            finally:
                main.mcp_manager.client_factory = old_factory
                main.DB_PATH = old_path

    def test_bulk_tool_toggle_enables_and_disables_all(self):
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            old_path, old_factory = main.DB_PATH, main.mcp_manager.client_factory
            main.DB_PATH = Path(directory) / "mcp-bulk.sqlite3"
            main.startup()
            client = FakeClient([{"name": "one", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}, {"name": "two", "inputSchema": {"type": "object"}}])
            main.mcp_manager.client_factory = lambda *_: client
            try:
                with TestClient(main.app) as http:
                    server = http.post("/api/mcp/servers", json={"name": "Bulk", "slug": "bulk", "endpoint": "https://mcp.example/mcp", "transport": "streamable-http", "enabled": True, "auth_type": "none"}).json()
                    http.post(f"/api/mcp/servers/{server['id']}/connect")
                    actions = {tool["remote_name"]: tool["action"] for tool in http.get("/api/mcp/servers").json()[0]["tools"]}
                    self.assertEqual(actions, {"one": "read_only", "two": "unknown"})
                    self.assertEqual(http.patch(f"/api/mcp/servers/{server['id']}/tools", json={"enabled": True}).status_code, 200)
                    self.assertTrue(all(tool["enabled"] for tool in http.get("/api/mcp/servers").json()[0]["tools"]))
                    tool = http.get("/api/mcp/servers").json()[0]["tools"][0]
                    classified = http.patch(f"/api/mcp/servers/{server['id']}/tools/{tool['id']}", json={"action": "read_only"})
                    self.assertEqual(classified.status_code, 200)
                    self.assertEqual(http.get("/api/mcp/servers").json()[0]["tools"][0]["action"], "read_only")
                    self.assertEqual(http.patch(f"/api/mcp/servers/{server['id']}/tools", json={"enabled": False}).status_code, 200)
                    self.assertFalse(any(tool["enabled"] for tool in http.get("/api/mcp/servers").json()[0]["tools"]))
                    self.assertEqual(http.patch(f"/api/mcp/servers/{server['id']}/tools", json={}).status_code, 422)
                    self.assertEqual(http.patch("/api/mcp/servers/missing/tools", json={"enabled": True}).status_code, 404)
            finally:
                main.mcp_manager.client_factory = old_factory
                main.DB_PATH = old_path

    def test_sdk_client_adds_bearer_header_only_when_configured(self):
        unauthenticated = MCPManager._sdk_client("https://mcp.example/mcp")
        authenticated = MCPManager._sdk_client("https://mcp.example/mcp", "example-secret")
        self.assertIsNone(unauthenticated.headers)
        self.assertEqual(authenticated.headers, {"Authorization": "Bearer example-secret"})

    def test_refresh_stable_identity_conservative_enable_and_call(self):
        repo, registry = Repo(), ToolRegistry()
        client = FakeClient([{"name": "query", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}], SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], structured_content={"rows": [[1]]}, is_error=False))
        manager = MCPManager(repo, lambda *_: client)
        manager.register(ModuleContext(None, {}, registry))
        asyncio.run(manager.refresh(repo.server_value))
        self.assertEqual(repo.items[0]["id"], "mcp.demo.query")
        self.assertFalse(registry.has("mcp.demo.query"))
        repo.items[0]["enabled"] = True
        repo.items[0]["action"] = "read_only"
        manager._sync_tools()
        result = asyncio.run(registry.invoke("mcp.demo.query", ToolExecutionContext("c", "p", "m", 1), {"q": "x"}))
        self.assertEqual(result["structured_data"], {"rows": [[1]]})
        self.assertEqual(result["content"], "ok")

    def test_refresh_uses_explicit_mcp_read_only_hint(self):
        repo, registry = Repo(), ToolRegistry()
        client = FakeClient([{"name": "search", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}])
        manager = MCPManager(repo, lambda *_: client)
        manager.register(ModuleContext(None, {}, registry))

        asyncio.run(manager.refresh(repo.server_value))

        self.assertEqual(repo.items[0]["action"], "read_only")

    def test_refresh_failure_keeps_stale_snapshot_but_unexposes_tools(self):
        repo = Repo()
        repo.items = [{"id": "mcp.demo.old", "server_id": "s1", "remote_name": "old", "description": "", "input_schema": '{"type":"object"}', "enabled": True}]
        manager = MCPManager(repo, lambda *_: FakeClient(error=OSError("offline")))
        registry = ToolRegistry()
        manager.register(ModuleContext(None, {}, registry))
        with self.assertRaises(OSError): asyncio.run(manager.refresh(repo.server_value))
        self.assertEqual(repo.items[0]["id"], "mcp.demo.old")
        self.assertEqual(repo.server_value["status"], "unreachable")
        self.assertFalse(registry.has("mcp.demo.old"))

    def test_timeout_and_malformed_schema(self):
        repo = Repo()
        tool = {"id": "mcp.demo.q", "remote_name": "q", "input_schema": '{"type":"object"}'}
        manager = MCPManager(repo, lambda *_: FakeClient(delay=.2))
        result = asyncio.run(manager.call(repo.server_value, tool, {}))
        self.assertEqual(result["error"]["code"], "invocation_timeout")
        with self.assertRaises(MCPProtocolError): normalized_schema({"type": "array"})

    def test_large_response_reports_truncation_without_leaking_sensitive_inputs(self):
        repo = Repo()
        result = SimpleNamespace(content=[SimpleNamespace(type="text", text="x" * 16000)], structured_content=None, is_error=False)
        manager = MCPManager(repo, lambda *_: FakeClient(result=result))
        response = asyncio.run(manager.call(repo.server_value, {"id": "mcp.demo.q", "remote_name": "q", "input_schema": '{"type":"object","properties":{"password":{"type":"string"}}}'}, {"password": "never-log"}))
        self.assertEqual(len(response["content"]), 16000)
        self.assertNotIn("never-log", json.dumps(repo.diagnostics))

    def test_structured_mcp_result_can_feed_native_artifact_and_datetime_tools_coexist(self):
        repo, registry = Repo(), ToolRegistry()
        client = FakeClient([{"name": "query", "inputSchema": {"type": "object"}}], SimpleNamespace(content=[], structured_content={"columns": ["day", "count"], "rows": [["2026-09-01", 4]]}, is_error=False))
        manager = MCPManager(repo, lambda *_: client)
        context = ModuleContext(None, {}, registry)
        register_native_tools(context)
        manager.register(context)
        asyncio.run(manager.refresh(repo.server_value))
        repo.items[0]["enabled"] = True
        repo.items[0]["action"] = "read_only"
        manager._sync_tools()
        execution = ToolExecutionContext("c", "p", "m", 1)
        result = asyncio.run(registry.invoke("mcp.demo.query", execution, {}))
        artifact = asyncio.run(registry.invoke("native.render_artifact", execution, {"type": "table", "title": "MCP data", "data": result["structured_data"]}))
        clock = asyncio.run(registry.invoke("native.get_current_datetime", execution, {"timezone": "UTC"}))
        self.assertEqual(artifact["artifacts"][0]["type"], "table")
        self.assertTrue(clock["iso"].endswith("+00:00"))

    def test_mcp_tool_uses_agent_runtime_and_tool_result_turn(self):
        repo, registry = Repo(), ToolRegistry()
        client = FakeClient([{"name": "query", "inputSchema": {"type": "object"}}], SimpleNamespace(content=[SimpleNamespace(type="text", text="rows")], structured_content={"rows": [[2]]}, is_error=False))
        manager = MCPManager(repo, lambda *_: client)
        manager.register(ModuleContext(None, {}, registry))
        asyncio.run(manager.refresh(repo.server_value))
        repo.items[0]["enabled"] = True
        repo.items[0]["action"] = "read_only"
        manager._sync_tools()

        class Adapter:
            model_id = "model"
            def __init__(self): self.messages = []
            async def stream(self, messages, tools, temperature=None):
                self.messages = messages
                if not any(message.get("role") == "tool" for message in messages):
                    yield ModelStreamChunk(tool_calls=[{"id": "call", "index": 0, "function": {"name": "mcp.demo.query", "arguments": "{}"}}])
                else:
                    yield ModelStreamChunk(content="done")
        adapter = Adapter()
        execution = ToolExecutionContext("c", "p", "model", 0)
        effective = ExposurePolicy().resolve(registry.catalog_view(), {"tool-calling"})
        request = AgentRunRequest(adapter, [{"role": "user", "content": "query"}], effective, ToolExecutor(), execution)
        events = asyncio.run(self.collect(AgentRuntime(), request))
        self.assertEqual(events[-1]["answer"], "done")
        self.assertIn('"structured_data":{"rows":[[2]]}', adapter.messages[-1]["content"])

    @staticmethod
    async def collect(runtime, request):
        return [event async for event in runtime.stream(request)]


if __name__ == "__main__": unittest.main()
