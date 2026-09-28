import asyncio
import json
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


class ChatToolTests(unittest.TestCase):
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
                self.assertEqual(run["status"], "completed")
                self.assertEqual([event["kind"] for event in runtime_events], ["REASON", "ACT", "REASON"])
                FakeClient.payloads = []
                response = asyncio.run(main.chat(main.ChatIn(provider_id="p", model_id="m", content="sin web", web_enabled=False, tools_enabled=False)))
                asyncio.run(self.collect(response.body_iterator))
                self.assertNotIn("tools", FakeClient.payloads[0])
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
