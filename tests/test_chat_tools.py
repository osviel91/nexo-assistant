import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.kernel import ToolDefinition


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    async def aiter_lines(self):
        if "tools" in self.payload and not any(message.get("role") == "tool" for message in self.payload["messages"]):
            chunks = [
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "web_search", "arguments": ""}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"query":"nexo"}'}}]}}]},
            ]
        else:
            chunks = [{"choices": [{"delta": {"content": "Respuesta con fuente [1]."}}]}]
        for chunk in chunks:
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"


class FakeStream:
    def __init__(self, payload):
        self.response = FakeResponse(payload)

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return None


class FakeClient:
    payloads = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, method, url, headers, json):
        self.payloads.append(json)
        return FakeStream(json)


class AgentBoundaryResponse:
    status_code = 200

    async def aiter_lines(self):
        yield 'data: ' + json.dumps({"choices": [{"delta": {"content": "ok"}}]})
        yield "data: [DONE]"


class AgentBoundaryStream:
    def __init__(self): self.response = AgentBoundaryResponse()
    async def __aenter__(self): return self.response
    async def __aexit__(self, *args): return None


class AgentBoundaryClient:
    payloads = []
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    def stream(self, method, url, headers, json):
        self.payloads.append(json)
        return AgentBoundaryStream()


class TextToolCallResponse:
    status_code = 200

    def __init__(self, payload): self.payload = payload

    async def aiter_lines(self):
        tool_results = [message for message in self.payload["messages"] if message.get("role") == "tool"]
        tool_result = tool_results[-1] if tool_results else None
        if tool_result is None:
            output = "<tool_call>\n<function=native.get_current_datetime>\n<parameter=timezone>Europe/Madrid</parameter>\n</function>\n</tool_call>"
        elif tool_result["name"] == "native.get_current_datetime":
            output = "<tool_call>\n<function=web_search>\n<parameter=query>Madrid forecast next days AEMET</parameter>\n</function>\n</tool_call>"
        elif tool_result["name"] == "web_search" and sum(item["name"] == "web_search" for item in tool_results) == 1:
            output = "<tool_call>\n<function=web_search>\n<parameter=query>Madrid forecast daily average temperature</parameter>\n</function>\n</tool_call>"
        elif tool_result["name"] == "web_search":
            result = json.loads(tool_result["content"])
            snippet = result["results"][0]["snippet"]
            labels = re.findall(r"\d+ Oct", snippet)
            values = [int(value) for value in re.findall(r"(\d+) C", snippet)]
            data = json.dumps({"labels": labels, "series": [{"name": "Temperatura media (°C)", "values": values}]})
            output = ("<tool_call>\n<function=native.render_artifact>\n<parameter=type>line</parameter>\n"
                      "<parameter=title>Temperatura media en Madrid</parameter>\n"
                      f"<parameter=data>{data}</parameter>\n</function>\n</tool_call>")
        else:
            output = "La previsión muestra temperaturas medias de 20, 22 y 21 °C para los próximos días."
        for chunk in (output,):
            yield "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]})
        yield "data: [DONE]"


class TextToolCallStream:
    def __init__(self, payload): self.response = TextToolCallResponse(payload)
    async def __aenter__(self): return self.response
    async def __aexit__(self, *args): pass


class TextToolCallClient:
    payloads = []
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def stream(self, method, url, headers, json):
        self.payloads.append(json)
        return TextToolCallStream(json)


class ChatToolTests(unittest.TestCase):
    def test_agent_mcp_tool_reaches_model_when_chat_toggle_is_enabled(self):
        from app import main
        from app.agent_profiles import AgentProfileInput
        from app.kernel import ToolDefinition
        from app.native_tools import register_native_tools

        with tempfile.TemporaryDirectory() as directory:
            old_db, old_client = main.DB_PATH, main.httpx.AsyncClient
            old_tools = main.module_registry.context.tools._tools.copy()
            main.DB_PATH = Path(directory) / "agent-mcp.sqlite3"
            main.startup()
            main.module_registry.context.tools._tools.clear()
            register_native_tools(main.module_registry.context)
            async def query(_context, _arguments): return {"content": "vault result"}
            main.module_registry.context.tools.register(ToolDefinition("mcp.obsidian.search", "Search vault", {"type": "object"}, query, "mcp", "mcp"))
            with main.db() as connection:
                connection.execute("INSERT INTO providers VALUES(?,?,?,?,?)", ("p", "Test", "http://provider", "", main.now()))
                connection.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?)", ("m", "p", "Model", '["tool-calling"]'))
            profile = main.agent_profiles.create(AgentProfileInput("Vault", provider_id="p", model_id="m", tool_names=("mcp.obsidian.search",)))
            AgentBoundaryClient.payloads.clear()
            main.httpx.AsyncClient = AgentBoundaryClient
            try:
                for enabled in (False, True):
                    response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="query", execution_mode="agent", agent_profile_id=profile["id"], tools_enabled=enabled)))
                    asyncio.run(self.collect(response.body_iterator))
            finally:
                main.httpx.AsyncClient = old_client
                main.DB_PATH = old_db
                main.module_registry.context.tools._tools.clear()
                main.module_registry.context.tools._tools.update(old_tools)

        self.assertNotIn("mcp.obsidian.search", [tool["function"]["name"] for tool in AgentBoundaryClient.payloads[0].get("tools", [])])
        self.assertIn("mcp.obsidian.search", [tool["function"]["name"] for tool in AgentBoundaryClient.payloads[1]["tools"]])

    def test_agent_provider_boundary_intrinsic_tools_selection_budget_and_snapshot(self):
        from app import main
        from app.agent_profiles import AgentProfileInput
        from app.kernel import ToolDefinition
        from app.native_tools import register_native_tools

        AgentBoundaryClient.payloads.clear()
        with tempfile.TemporaryDirectory() as directory:
            old_db, old_client = main.DB_PATH, main.httpx.AsyncClient
            old_tools = main.module_registry.context.tools._tools.copy()
            main.DB_PATH = Path(directory) / "agent-boundary.sqlite3"
            main.startup()
            main.module_registry.context.tools._tools.clear()
            register_native_tools(main.module_registry.context)
            async def search(_context, _arguments): return {"results": []}
            main.module_registry.context.tools.register(ToolDefinition("web_search", "search", {"type": "object"}, search, "module", "web_search", action="read_only"))
            with main.db() as connection:
                connection.execute("INSERT INTO providers VALUES(?,?,?,?,?)", ("p", "Test", "http://provider", "", main.now()))
                connection.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?)", ("m", "p", "Model", '["tool-calling"]'))
                connection.executemany("INSERT INTO notebooks VALUES(?,?,?,?,?)", [("n1", "One", "", main.now(), main.now()), ("n2", "Two", "", main.now(), main.now())])
            profile = main.agent_profiles.create(AgentProfileInput("Research", provider_id="p", model_id="m", tool_names=("web_search",), max_tool_calls=12))
            restricted = main.agent_profiles.create(AgentProfileInput("Restricted", provider_id="p", model_id="m", max_tool_calls=4))
            multi = main.agent_profiles.create(AgentProfileInput("Multi", provider_id="p", model_id="m", notebook_ids=("n1", "n2")))
            main.httpx.AsyncClient = AgentBoundaryClient
            try:
                for selected in (profile, restricted):
                    response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="test", execution_mode="agent", agent_profile_id=selected["id"])))
                    asyncio.run(self.collect(response.body_iterator))
                research_tools = [tool["function"]["name"] for tool in AgentBoundaryClient.payloads[0]["tools"]]
                restricted_tools = [tool["function"]["name"] for tool in AgentBoundaryClient.payloads[1]["tools"]]
                with main.db() as connection:
                    message = connection.execute("SELECT runtime_metadata FROM messages WHERE role='assistant' ORDER BY rowid LIMIT 1").fetchone()
                    snapshot = json.loads(message[0])
                main.module_registry.context.tools.unregister("web_search")
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="offline", execution_mode="agent", agent_profile_id=profile["id"])))
                offline_body = asyncio.run(self.collect(response.body_iterator))
                offline_done = next(json.loads(line[6:]) for line in offline_body.splitlines() if line.startswith("data: ") and json.loads(line[6:]).get("done"))
                self.assertIn("web_search", main.agent_profiles.get(profile["id"])["tool_names"])
                self.assertIn("web_search", offline_done["runtime"]["requested_tool_names"])
                self.assertNotIn("web_search", offline_done["runtime"]["available_tool_names"])
                self.assertEqual(offline_done["runtime"]["excluded_tool_reasons"]["web_search"], "not_registered_or_unavailable")
                main.module_registry.context.tools.register(ToolDefinition("web_search", "search", {"type": "object"}, search, "module", "web_search", action="read_only"))
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="online", execution_mode="agent", agent_profile_id=profile["id"])))
                asyncio.run(self.collect(response.body_iterator))
                with self.assertRaises(main.HTTPException) as conflict:
                    asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="ambiguous", execution_mode="agent", agent_profile_id=multi["id"])))
                self.assertEqual(conflict.exception.status_code, 409)
                updated = main.agent_profiles.update(profile["id"], {"name": "Edited", "tool_names": (), "max_tool_calls": 2})
                self.assertEqual(updated["max_tool_calls"], 2)
                with main.db() as connection:
                    historical = json.loads(connection.execute("SELECT runtime_metadata FROM messages WHERE role='assistant' ORDER BY rowid LIMIT 1").fetchone()[0])
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="explicit", execution_mode="agent", agent_profile_id=multi["id"], notebook_id="n2")))
                explicit_body = asyncio.run(self.collect(response.body_iterator))
                explicit_done = next(json.loads(line[6:]) for line in explicit_body.splitlines() if line.startswith("data: ") and json.loads(line[6:]).get("done"))
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="inherited", conversation_id=explicit_done["conversation_id"])))
                inherited_body = asyncio.run(self.collect(response.body_iterator))
                inherited_done = next(json.loads(line[6:]) for line in inherited_body.splitlines() if line.startswith("data: ") and json.loads(line[6:]).get("done"))
            finally:
                main.httpx.AsyncClient = old_client
                main.DB_PATH = old_db
                main.module_registry.context.tools._tools.clear()
                main.module_registry.context.tools._tools.update(old_tools)

        native = ["native.get_current_datetime", "native.render_artifact", "native.request_user_input"]
        self.assertCountEqual(research_tools, ["web_search", *native])
        self.assertCountEqual(restricted_tools, native)
        self.assertCountEqual([tool["function"]["name"] for tool in AgentBoundaryClient.payloads[2]["tools"]], native)
        self.assertCountEqual([tool["function"]["name"] for tool in AgentBoundaryClient.payloads[3]["tools"]], ["web_search", *native])
        self.assertEqual(snapshot["max_tool_calls"], 12)
        self.assertEqual(snapshot["agent_profile_name"], "Research")
        self.assertEqual(snapshot["effective_tool_names"], historical["effective_tool_names"])
        self.assertEqual(historical["max_tool_calls"], 12)
        self.assertEqual(historical["resolved_model"], "m")
        self.assertEqual(explicit_done["runtime"]["notebook_id"], "n2")
        self.assertEqual(inherited_done["runtime"]["notebook_id"], "n2")

    def test_chart_series_allow_missing_values_except_for_pie(self):
        from app.artifacts import ArtifactError, validate_artifact

        base = {"title": "Temperaturas", "data": {"labels": ["Jue", "Vie", "Sáb"], "series": [{"name": "Máxima", "values": [24, 22, None]}]}}
        self.assertEqual(validate_artifact({**base, "type": "line"})["data"]["series"][0]["values"], [24, 22, None])
        self.assertEqual(validate_artifact({**base, "type": "bar"})["data"]["series"][0]["values"], [24, 22, None])
        with self.assertRaises(ArtifactError):
            validate_artifact({**base, "type": "pie"})

    def test_tool_round_limit_is_persisted_and_used_by_agent_runtime(self):
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            old_db = main.DB_PATH
            main.DB_PATH = Path(directory) / "tool-settings.sqlite3"
            main.startup()
            try:
                with TestClient(main.app) as client:
                    self.assertEqual(client.get("/api/settings/tools").json(), {"max_tool_calls": 10, "has_aemet_api_key": False})
                    saved = client.put("/api/settings/tools", json={"max_tool_calls": 9})
                    self.assertEqual(saved.status_code, 200)
                    self.assertEqual(client.get("/api/settings/tools").json(), {"max_tool_calls": 9, "has_aemet_api_key": False})
                    self.assertEqual(main.configured_agent_runtime().limits.max_tool_calls, 9)
                    self.assertEqual(client.put("/api/settings/tools", json={"max_tool_calls": 51}).status_code, 422)
            finally:
                main.DB_PATH = old_db

    def test_date_two_searches_and_chart_flow_end_to_end(self):
        from app import main
        from app.native_tools import register_native_tools

        with tempfile.TemporaryDirectory() as directory:
            old_db, old_client = main.DB_PATH, main.httpx.AsyncClient
            old_tools = main.module_registry.context.tools._tools.copy()
            main.DB_PATH = Path(directory) / "text-tools.sqlite3"
            main.startup()
            with main.db() as connection:
                connection.execute("INSERT INTO providers VALUES(?,?,?,?,?)", ("p", "Test", "http://provider", "", main.now()))
                connection.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?)", ("m", "p", "m", '["tool-calling"]'))
            main.module_registry.context.tools._tools.clear()
            register_native_tools(main.module_registry.context)

            async def search(_context, _arguments):
                return {"results": [{"title": "Pronóstico AEMET", "url": "https://weather.test/madrid", "snippet": "1 Oct 20 C; 2 Oct 22 C; 3 Oct 21 C"}]}

            main.module_registry.context.tools.register(ToolDefinition("web_search", "search", {"type": "object"}, search, action="read_only"))
            from app.agent_profiles import AgentProfileInput
            profile = main.agent_profiles.create(AgentProfileInput("Research", provider_id="p", model_id="m", tool_names=("web_search",), max_tool_calls=12))
            TextToolCallClient.payloads = []
            main.httpx.AsyncClient = TextToolCallClient
            try:
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="grafica la temperatura promedio para madrid para los próximos días", execution_mode="agent", agent_profile_id=profile["id"], web_enabled=True, tools_enabled=True)))
                body = asyncio.run(self.collect(response.body_iterator))
                with main.db() as connection:
                    run = connection.execute("SELECT status FROM runtime_runs ORDER BY started_at DESC LIMIT 1").fetchone()
                    message = connection.execute("SELECT content,runtime_metadata,artifacts FROM messages WHERE role='assistant'").fetchone()
            finally:
                main.httpx.AsyncClient = old_client
                main.DB_PATH = old_db
                main.module_registry.context.tools._tools.clear()
                main.module_registry.context.tools._tools.update(old_tools)

        self.assertEqual(run["status"], "completed")
        runtime, artifacts = json.loads(message["runtime_metadata"]), json.loads(message["artifacts"])
        self.assertEqual(runtime["tools_used"], ["native.get_current_datetime", "web_search", "web_search", "native.render_artifact"])
        self.assertEqual(artifacts[0]["type"], "line")
        self.assertEqual(artifacts[0]["data"]["series"][0]["values"], [20, 22, 21])
        self.assertIn("20, 22 y 21", message["content"])
        self.assertNotIn("<tool_call>", body)
        self.assertEqual(len(TextToolCallClient.payloads), 5)
        search_result = next(message for message in TextToolCallClient.payloads[3]["messages"] if message.get("role") == "tool" and message.get("name") == "web_search")
        self.assertIn("1 Oct 20 C", search_result["content"])
        call_ids = [call["id"] for message in TextToolCallClient.payloads[-1]["messages"] for call in message.get("tool_calls", [])]
        self.assertEqual(len(call_ids), len(set(call_ids)))

    def test_compatible_model_gets_tool_and_persists_cited_sources(self):
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            old_db = main.DB_PATH
            main.DB_PATH = Path(directory) / "test.sqlite3"
            main.startup()
            with main.db() as connection:
                connection.execute("INSERT INTO providers VALUES(?,?,?,?,?)", ("p", "Test", "http://provider", "", main.now()))
                connection.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?)", ("m", "p", "m", '["tool-calling"]'))
            async def search(_context, arguments):
                return {"results": [{"title": "Nexo", "url": "https://nexo.test", "snippet": "Nexo"}]}
            main.module_registry.context.tools._tools.clear()
            main.module_registry.context.tools.register(ToolDefinition("web_search", "search", {"type": "object"}, search, action="read_only"))
            FakeClient.payloads = []
            old_client = main.httpx.AsyncClient
            main.httpx.AsyncClient = FakeClient
            try:
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="busca")))
                body = asyncio.run(self.collect(response.body_iterator))
                compatible_payload = FakeClient.payloads[0]
                tool_result_payload = FakeClient.payloads[1]
                with main.db() as connection:
                    run = connection.execute("SELECT * FROM runtime_runs ORDER BY started_at DESC LIMIT 1").fetchone()
                    runtime_events = connection.execute("SELECT kind FROM runtime_events WHERE run_id=? ORDER BY sequence", (run["id"],)).fetchall()
                    saved_runtime = json.loads(connection.execute("SELECT runtime_metadata FROM messages WHERE role='assistant' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
                self.assertEqual(run["status"], "completed")
                self.assertEqual([event["kind"] for event in runtime_events], ["tool_selection_started", "REASON", "TOOL_REQUESTED", "ACT", "TOOL_POLICY", "TOOL_EXECUTION_STARTED", "TOOL_EXECUTION_COMPLETED", "TOOL_RESULT_PROJECTED", "PROGRESS_EVALUATED", "REASON", "PROVIDER_CONTINUATION_STARTED"])
                self.assertEqual(saved_runtime["tools_used"], ["web_search"])
                FakeClient.payloads = []
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="sin web", web_enabled=False, tools_enabled=False)))
                asyncio.run(self.collect(response.body_iterator))
                self.assertNotIn("tools", FakeClient.payloads[0])
                with main.db() as connection:
                    no_tools_runtime = json.loads(connection.execute("SELECT runtime_metadata FROM messages WHERE role='assistant' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
                self.assertEqual(no_tools_runtime["tools_used"], [])
                FakeClient.payloads = []
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="solo web", web_enabled=True, tools_enabled=False)))
                asyncio.run(self.collect(response.body_iterator))
                self.assertEqual(FakeClient.payloads[0]["tools"][0]["function"]["name"], "web_search")
                with main.db() as connection:
                    connection.execute("UPDATE models SET capabilities='[]' WHERE provider_id='p' AND id='m'")
                FakeClient.payloads = []
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="sin herramientas")))
                asyncio.run(self.collect(response.body_iterator))
                with main.db() as connection:
                    capability_runtime = json.loads(connection.execute("SELECT runtime_metadata FROM messages WHERE role='assistant' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
                FakeClient.payloads = []
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="adjunto", attachments=[{"kind": "text", "name": "note.txt", "text": "contexto"}])))
                asyncio.run(self.collect(response.body_iterator))
                attachment_payload = FakeClient.payloads[0]
                with main.db() as connection:
                    stored_attachment = connection.execute("SELECT attachments FROM messages WHERE content='adjunto'").fetchone()[0]
            finally:
                main.httpx.AsyncClient = old_client
                main.DB_PATH = old_db
            self.assertTrue("tools" in compatible_payload)
            self.assertEqual(compatible_payload["tools"][0]["type"], "function")
            self.assertEqual(compatible_payload["tools"][0]["function"]["name"], "web_search")
            self.assertEqual(compatible_payload["tools"][0]["function"]["parameters"], {"type": "object"})
            self.assertFalse(capability_runtime["model_tool_calling_supported"])
            self.assertTrue(capability_runtime["web_tools_enabled"])
            self.assertTrue(capability_runtime["tools_enabled"])
            self.assertEqual(capability_runtime["registered_tool_names"], ["web_search"])
            self.assertEqual(capability_runtime["effective_tool_names"], [])
            self.assertEqual(capability_runtime["tool_availability_reason"], "model_capability_not_enabled")
            self.assertIn('"sources": [{"title": "Nexo", "url": "https://nexo.test"}]', body)
            self.assertIn("Respuesta con fuente", body)
            self.assertEqual(tool_result_payload["messages"][-1]["role"], "tool")
            self.assertNotIn("tools", FakeClient.payloads[0])
            self.assertEqual(attachment_payload["messages"][-1]["content"][1]["text"], "\n\n[Archivo: note.txt ]\ncontexto")
            self.assertIn("note.txt", stored_attachment)

    async def collect(self, iterator):
        chunks = []
        async for chunk in iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)


if __name__ == "__main__":
    unittest.main()
