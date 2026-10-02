import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.agent_profiles import AgentProfileInput, AgentProfileResolver, AgentProfileRepository, AgentProfileService, ProfileNotFoundError, ProfileResolutionError
from app.migrations import migrate


class Stage6BProfileRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nexo.sqlite3"

        def connection():
            import sqlite3
            result = sqlite3.connect(self.path)
            result.row_factory = sqlite3.Row
            result.execute("PRAGMA foreign_keys=ON")
            return result

        self.connection = connection
        with connection() as db:
            migrate(db)
            db.execute("INSERT INTO providers VALUES ('p', 'Provider', 'http://provider', 'provider-secret', 'now')")
            db.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES ('m','p','Model','[\"tool-calling\"]')")
            db.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES ('m2','p','Model 2','[]')")
        self.service = AgentProfileService(AgentProfileRepository(connection, lambda: "now"), lambda: [])
        self.resolver = AgentProfileResolver(self.service.repository)

    def tearDown(self):
        self.directory.cleanup()

    def profile(self, **overrides):
        values = {"name": "Research", "provider_id": "p", "model_id": "m", "system_instructions": "Be brief", "model_parameters": {"temperature": 0.7}, "tool_names": ()}
        values.update(overrides)
        return AgentProfileInput(**values)

    def test_resolver_snapshots_precedence_and_empty_tools(self):
        profile = self.service.create(self.profile(model_parameters={}))
        config = self.resolver.resolve(profile["id"], runtime_temperature=0.2)
        self.assertEqual(config.temperature, 0.2)
        self.assertEqual(config.requested_tool_names, ())
        updated = self.service.update(profile["id"], {"system_instructions": "changed", "model_parameters": {"temperature": 1.2}})
        self.assertEqual(config.system_instructions, "Be brief")
        self.assertEqual(config.temperature, 0.2)
        self.assertNotEqual(updated["system_instructions"], config.system_instructions)

    def test_resolver_reports_missing_and_invalid_profiles_safely(self):
        with self.assertRaises(ProfileNotFoundError):
            self.resolver.resolve("deleted")
        profile = self.service.create(self.profile())
        with self.connection() as db:
            db.execute("DELETE FROM models")
        with self.assertRaises(ProfileResolutionError) as error:
            self.resolver.resolve(profile["id"])
        self.assertEqual(str(error.exception), "agent_model_unavailable")

    def test_switching_profiles_changes_resolved_physical_pair_and_clearing_uses_default(self):
        agent_a = self.service.create(self.profile(name="A", model_id="m"))
        agent_b = self.service.create(self.profile(name="B", model_id="m2"))
        a = self.resolver.resolve(agent_a["id"])
        b = self.resolver.resolve(agent_b["id"])
        self.assertEqual((a.provider_id, a.model_id), ("p", "m"))
        self.assertEqual((b.provider_id, b.model_id), ("p", "m2"))

    def test_profile_chat_selects_model_system_temperature_limited_tools_and_safe_trace(self):
        from app import main
        from app.kernel import ToolDefinition

        old_db, old_tools = main.DB_PATH, main.module_registry.context.tools._tools.copy()
        old_profiles, old_resolver = main.agent_profiles, main.agent_profile_resolver
        main.DB_PATH = self.path
        seen = []

        async def visible(_context, arguments):
            seen.append(arguments)
            return {"ok": True}

        main.module_registry.context.tools._tools.clear()
        main.module_registry.context.tools.register(ToolDefinition("visible", "Visible", {"type": "object"}, visible, action="read_only"))
        main.module_registry.context.tools.register(ToolDefinition("hidden", "Hidden", {"type": "object"}, visible, "module", "hidden", action="read_only"))
        main.module_registry.context.tools.register(ToolDefinition("native.get_current_datetime", "Clock", {"type": "object"}, visible, "native", "native", action="read_only"))
        main.module_registry.context.tools.register(ToolDefinition("native.render_artifact", "Renderer", {"type": "object"}, visible, "native", "native", action="read_only"))
        main.module_registry.context.tools.register(ToolDefinition("aemet.opendata", "Weather", {"type": "object"}, visible, "module", "aemet", action="read_only"))
        main.module_registry.context.tools.register(ToolDefinition("mcp.demo.query", "MCP", {"type": "object"}, visible, "mcp", "mcp", action="read_only"))
        main.agent_profiles = AgentProfileService(AgentProfileRepository(main.db, main.now), main.module_registry.tool_catalog)
        main.agent_profile_resolver = AgentProfileResolver(main.agent_profiles.repository)
        profile = main.agent_profiles.create(self.profile(tool_names=("visible", "aemet.opendata", "mcp.demo.query", "missing__tool")))
        self.assertEqual(main.agent_profile_resolver.resolve(profile["id"]).requested_tool_names, ("aemet.opendata", "mcp.demo.query", "missing__tool", "visible"))

        class Response:
            status_code = 200

            async def aiter_lines(self):
                if self.payload.get("tools") and not any(message.get("role") == "tool" for message in self.payload["messages"]):
                    chunks = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call", "function": {"name": "visible", "arguments": "{}"}}]}}]}]
                else:
                    chunks = [{"choices": [{"delta": {"content": "continued"}}]}]
                for chunk in chunks:
                    yield "data: " + json.dumps(chunk)
                yield "data: [DONE]"

        class Stream:
            def __init__(self, payload):
                self.response = Response()
                self.response.payload = payload

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, *args):
                return None

        class Client:
            payloads = []
            calls = []

            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, method, url, headers, json):
                self.payloads.append(json)
                self.calls.append((method, url, headers))
                return Stream(json)

        old_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = Client
        try:
            response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="wrong", content="run", temperature=0.1, agent_profile_id=profile["id"])))
            body = asyncio.run(self.collect(response.body_iterator))
            payload = Client.payloads[0]
            self.assertEqual(Client.calls[0][1], "http://provider/chat/completions")
            self.assertEqual(payload["model"], "m")
            self.assertEqual(payload["temperature"], 0.7)
            self.assertEqual(payload["messages"][0], {"role": "system", "content": "Be brief"})
            self.assertEqual([tool["function"]["name"] for tool in payload["tools"]], ["visible", "native.get_current_datetime", "native.render_artifact", "aemet.opendata", "mcp.demo.query"])
            done = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ") and json.loads(line[6:]).get("done")][0]
            self.assertEqual(done["model_id"], "m")
            self.assertEqual(done["runtime"]["agent_profile_name"], "Research")
            self.assertEqual(done["runtime"]["resolved_provider"], "p")
            self.assertEqual(done["runtime"]["resolved_model"], "m")
            self.assertTrue(done["runtime"]["system_instructions_applied"])
            self.assertEqual(seen, [{}])
            self.assertIn("continued", body)
            Client.payloads.clear()
            response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="wrong", content="tools disabled", agent_profile_id=profile["id"], tools_enabled=False)))
            asyncio.run(self.collect(response.body_iterator))
            self.assertNotIn("tools", Client.payloads[0])
            with main.db() as db:
                db.execute("UPDATE models SET capabilities='[]' WHERE provider_id='p' AND id='m'")
            Client.payloads.clear()
            response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="wrong", content="no model support", agent_profile_id=profile["id"], tools_enabled=True)))
            asyncio.run(self.collect(response.body_iterator))
            self.assertNotIn("tools", Client.payloads[0])
            with main.db() as db:
                run = db.execute("SELECT * FROM runtime_runs ORDER BY started_at DESC LIMIT 1").fetchone()
                assistant = db.execute("SELECT * FROM messages WHERE role='assistant' ORDER BY created_at DESC LIMIT 1").fetchone()
            metadata = json.loads(run["metadata"])
            self.assertEqual(metadata["agent_profile_id"], profile["id"])
            self.assertEqual(json.loads(assistant["runtime_metadata"])["agent_profile_name"], "Research")
            self.assertNotIn("Be brief", json.dumps(metadata))
            self.assertNotIn("provider-secret", json.dumps(metadata))
        finally:
            main.httpx.AsyncClient = old_client
            main.DB_PATH = old_db
            main.agent_profiles, main.agent_profile_resolver = old_profiles, old_resolver
            main.module_registry.context.tools._tools.clear()
            main.module_registry.context.tools._tools.update(old_tools)

    async def collect(self, iterator):
        chunks = []
        async for chunk in iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)


if __name__ == "__main__":
    unittest.main()
