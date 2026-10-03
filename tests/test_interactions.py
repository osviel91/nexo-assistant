import json
import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.interactions import normalize_dialog, validate_values


class InteractionTests(unittest.TestCase):
    def test_structured_fields_validate_choices_and_defaults(self):
        dialog = normalize_dialog({"title": "Choose", "fields": [{
            "id": "target", "label": "Target", "type": "select", "required": True,
            "options": [{"label": "Vault A", "value": "a"}, {"label": "Vault B", "value": "b"}], "default": "a",
        }]})
        self.assertEqual(validate_values(dialog["fields"], {"target": "b"}), {"target": "b"})
        with self.assertRaises(ValueError):
            validate_values(dialog["fields"], {"target": "other"})
        with self.assertRaises(ValueError):
            normalize_dialog({"title": "Bad", "fields": [{"id": "n", "type": "number", "default": float("nan")}]})

    def test_interaction_endpoint_requires_explicit_approval_and_validates_values(self):
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            old_db = main.DB_PATH
            main.DB_PATH = Path(directory) / "interactions.sqlite3"
            try:
                main.startup()
                conversation_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
                with main.db() as connection:
                    connection.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)", (conversation_id, "Test", main.now(), main.now()))
                    connection.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status,metadata) VALUES(?,?,?,?,?,?)", (run_id, conversation_id, "message", main.now(), "running", "{}"))
                interaction_id = main.create_runtime_interaction(run_id, conversation_id, {
                    "kind": "tool_approval", "tool": "vault.write", "payload": {"title": "Approve", "fields": []},
                    "validation_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
                })
                with TestClient(main.app) as client:
                    self.assertEqual(client.post(f"/api/interactions/{interaction_id}", json={"values": {"path": "note.md"}}).status_code, 422)
                    self.assertEqual(client.post(f"/api/interactions/{interaction_id}", json={"approved": 1, "values": {"arguments": {"path": "note.md"}}}).status_code, 422)
                    self.assertEqual(client.post(f"/api/interactions/{interaction_id}", json={"approved": True, "values": {"arguments": {"path": 123}}}).status_code, 422)
                    self.assertEqual(client.post(f"/api/interactions/{interaction_id}", json={"approved": True, "session_approved": True, "values": {"arguments": {"path": "edited.md"}}}).status_code, 200)
                    self.assertEqual(client.post(f"/api/interactions/{interaction_id}", json={"approved": True, "values": {"arguments": {"path": "again.md"}}}).status_code, 409)
                with main.db() as connection:
                    row = connection.execute("SELECT status,response FROM runtime_interactions WHERE id=?", (interaction_id,)).fetchone()
                self.assertEqual(row["status"], "approved")
                self.assertEqual(json.loads(row["response"])["values"], {"path": "edited.md"})
                with main.db() as connection:
                    self.assertEqual(connection.execute("SELECT tool_name FROM conversation_tool_approvals WHERE conversation_id=?", (conversation_id,)).fetchone()[0], "vault.write")
            finally:
                main.DB_PATH = old_db

    def test_chat_pauses_for_editable_approval_then_resumes_tool_call(self):
        from app import main
        from app.agent_profiles import AgentProfileInput
        from app.kernel import ToolDefinition
        from app.native_tools import register_native_tools

        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            async def aiter_lines(self):
                if not any(message.get("role") == "tool" for message in self.payload["messages"]):
                    delta = {"tool_calls": [{"index": 0, "id": "call", "function": {"name": "vault.write", "arguments": '{"path":"model.md"}'}}]}
                else:
                    delta = {"content": "Saved the note."}
                yield "data: " + json.dumps({"choices": [{"delta": delta}]})
                yield "data: [DONE]"

        class Stream:
            def __init__(self, payload):
                self.response = Response(payload)

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, *args):
                return None

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, method, url, headers, json):
                return Stream(json)

        with tempfile.TemporaryDirectory() as directory:
            old_db, old_client = main.DB_PATH, main.httpx.AsyncClient
            old_tools = main.module_registry.context.tools._tools.copy()
            main.DB_PATH = Path(directory) / "chat-interaction.sqlite3"
            main.startup()
            main.module_registry.context.tools._tools.clear()
            register_native_tools(main.module_registry.context)
            executed = []

            async def write(_context, arguments):
                executed.append(arguments)
                return {"ok": True}

            main.module_registry.context.tools.register(ToolDefinition(
                "vault.write", "Write a note", {"type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False}, write, "mcp", "mcp", action="mutating",
            ))
            with main.db() as connection:
                connection.execute("INSERT INTO providers VALUES(?,?,?,?,?)", ("p", "Test", "http://provider", "", main.now()))
                connection.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?)", ("m", "p", "Model", '["tool-calling"]'))
            profile = main.agent_profiles.create(AgentProfileInput("Writer", provider_id="p", model_id="m", tool_names=("vault.write",)))
            main.httpx.AsyncClient = Client
            try:
                async def collect():
                    response = await main.chat(main.ChatIn(provider_id="p", model_id="m", content="write", execution_mode="agent", agent_profile_id=profile["id"]))
                    events = []
                    async for part in response.body_iterator:
                        for line in part.splitlines():
                            if not line.startswith("data: "):
                                continue
                            event = json.loads(line[6:])
                            events.append(event)
                            if "interaction" in event:
                                self.assertEqual(executed, [])
                                approval = main.RuntimeInteractionInput(approved=True, values={"arguments": '{"path":"edited.md"}'})
                                main.respond_to_runtime_interaction(event["interaction"]["id"], approval)
                    return events

                events = asyncio.run(collect())
                self.assertEqual(executed, [{"path": "edited.md"}])
                self.assertTrue(any(event.get("done") for event in events))
                with main.db() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM runtime_interactions").fetchone()[0], 0)
            finally:
                main.httpx.AsyncClient = old_client
                main.DB_PATH = old_db
                main.module_registry.context.tools._tools.clear()
                main.module_registry.context.tools._tools.update(old_tools)


if __name__ == "__main__":
    unittest.main()
