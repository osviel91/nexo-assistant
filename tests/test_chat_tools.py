import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path

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


class TextToolCallResponse:
    status_code = 200

    def __init__(self, payload): self.payload = payload

    async def aiter_lines(self):
        tool_result = next((message for message in reversed(self.payload["messages"]) if message.get("role") == "tool"), None)
        if tool_result is None:
            output = "<tool_call>\n<function=web_search>\n<parameter=query>Madrid forecast próximos días</parameter>\n</function>\n</tool_call>"
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
    def test_text_tool_calls_search_results_and_chart_flow_end_to_end(self):
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

            main.module_registry.context.tools.register(ToolDefinition("web_search", "search", {"type": "object"}, search))
            TextToolCallClient.payloads = []
            main.httpx.AsyncClient = TextToolCallClient
            try:
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="grafica la temperatura promedio para madrid para los próximos días", web_enabled=True, tools_enabled=True)))
                body = asyncio.run(self.collect(response.body_iterator))
                with main.db() as connection:
                    message = connection.execute("SELECT content,runtime_metadata,artifacts FROM messages WHERE role='assistant'").fetchone()
                runtime, artifacts = json.loads(message["runtime_metadata"]), json.loads(message["artifacts"])
            finally:
                main.httpx.AsyncClient = old_client
                main.DB_PATH = old_db
                main.module_registry.context.tools._tools.clear()
                main.module_registry.context.tools._tools.update(old_tools)

        self.assertEqual(runtime["tools_used"], ["web_search", "native.render_artifact"])
        self.assertEqual(artifacts[0]["type"], "line")
        self.assertEqual(artifacts[0]["data"]["series"][0]["values"], [20, 22, 21])
        self.assertIn("20, 22 y 21", message["content"])
        self.assertNotIn("<tool_call>", body)
        self.assertEqual(len(TextToolCallClient.payloads), 3)
        search_result = next(message for message in TextToolCallClient.payloads[1]["messages"] if message.get("role") == "tool" and message.get("name") == "web_search")
        self.assertIn("1 Oct 20 C", search_result["content"])

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
            main.module_registry.context.tools.register(ToolDefinition("web_search", "search", {"type": "object"}, search))
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
                self.assertEqual([event["kind"] for event in runtime_events], ["REASON", "ACT", "REASON"])
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
