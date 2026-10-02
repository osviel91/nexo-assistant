import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_profiles import AgentProfileInput, AgentProfileRepository, AgentProfileResolver, AgentProfileService, ProfileValidationError
from app.migrations import migrate


class AgentProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nexo.sqlite3"

        def connection():
            result = sqlite3.connect(self.path)
            result.row_factory = sqlite3.Row
            result.execute("PRAGMA foreign_keys=ON")
            return result

        self.connection = connection
        with connection() as db:
            migrate(db)
            db.execute("INSERT INTO providers VALUES ('provider', 'Private', 'http://provider', 'secret-key', 'now')")
            db.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES ('model','provider','Model','[]')")
        self.service = AgentProfileService(AgentProfileRepository(connection, lambda: "now"), lambda: [
            {"name": "web_search", "description": "safe", "parameters": {}, "source": "native"},
            {"name": "mcp__server__tool", "description": "safe", "parameters": {}, "source": "mcp"},
        ])

    def tearDown(self):
        self.directory.cleanup()

    def profile(self, **overrides):
        values = {"name": "Research", "provider_id": "provider", "model_id": "model",
                  "system_instructions": "Be concise", "model_parameters": {"temperature": 0.4},
                  "tool_names": ("web_search", "mcp__server__tool")}
        values.update(overrides)
        return AgentProfileInput(**values)

    def test_create_read_list_update_delete(self):
        created = self.service.create(self.profile())
        self.assertEqual(created["model_parameters"], {"temperature": 0.4})
        self.assertEqual(created["unavailable_tools"], [])
        self.assertEqual(self.service.get(created["id"])["name"], "Research")
        self.assertEqual(len(self.service.list()), 1)
        updated = self.service.update(created["id"], {"name": "Updated", "tool_names": ("web_search",)})
        self.assertEqual(updated["name"], "Updated")
        self.service.delete(created["id"])
        self.assertEqual(self.service.list(), [])

    def test_duplicate_names_are_allowed(self):
        self.service.create(self.profile())
        self.service.create(self.profile())
        self.assertEqual(len(self.service.list()), 2)

    def test_reference_and_parameter_validation(self):
        for values in ({"provider_id": "missing"}, {"model_id": "missing"}, {"model_parameters": {"top_p": 0.5}},
                       {"model_parameters": {"temperature": 3}}, {"tool_names": ("not valid",)}):
            with self.assertRaises(ProfileValidationError):
                self.service.create(self.profile(**values))

    def test_deleted_model_is_reported_unavailable(self):
        created = self.service.create(self.profile())
        with self.connection() as db:
            db.execute("DELETE FROM models WHERE provider_id='provider' AND id='model'")
        self.assertFalse(self.service.get(created["id"])["model_available"])

    def test_unavailable_tool_is_preserved(self):
        created = self.service.create(self.profile(tool_names=("web_search", "mcp__missing__tool")))
        result = self.service.get(created["id"])
        self.assertEqual(result["tool_names"], ["mcp__missing__tool", "web_search"])
        self.assertEqual(result["unavailable_tools"], ["mcp__missing__tool"])

    def test_dotted_registry_tool_names_save_and_resolve(self):
        created = self.service.create(self.profile(tool_names=("native.get_current_datetime", "mcp.demo.query")))
        resolved = AgentProfileResolver(self.service.repository).resolve(created["id"])
        self.assertEqual(set(resolved.requested_tool_names), {"native.get_current_datetime", "mcp.demo.query"})

    def test_notebook_bindings_and_runtime_budget_persist_and_resolve(self):
        with self.connection() as db:
            db.executemany("INSERT INTO notebooks VALUES(?,?,?,?,?)", [("n1", "One", "", "now", "now"), ("n2", "Two", "", "now", "now")])
        created = self.service.create(self.profile(notebook_ids=("n2", "n1"), max_tool_calls=12))
        self.assertEqual(created["notebook_ids"], ["n1", "n2"])
        resolved = AgentProfileResolver(self.service.repository).resolve(created["id"])
        self.assertEqual(resolved.requested_notebook_ids, ("n1", "n2"))
        self.assertEqual(resolved.max_tool_calls, 12)
        updated = self.service.update(created["id"], {"notebook_ids": ("n2",), "max_tool_calls": 7})
        self.assertEqual(updated["notebook_ids"], ["n2"])
        self.assertEqual(updated["max_tool_calls"], 7)
        with self.connection() as db:
            db.execute("DELETE FROM notebooks WHERE id='n2'")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM agent_profile_notebooks WHERE agent_profile_id=?", (created["id"],)).fetchone()[0], 0)

    def test_api_contract_does_not_expose_provider_secret_or_tool_schema(self):
        import app.main as main
        old_path, old_service = main.DB_PATH, main.agent_profiles
        main.DB_PATH = self.path
        main.agent_profiles = self.service
        try:
            with TestClient(main.app) as client:
                response = client.post("/api/agents", json={"name": "API", "provider_id": "provider", "model_id": "model", "tool_names": ["web_search"]})
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertNotIn("secret-key", response.text)
                self.assertNotIn("parameters", payload)
                self.assertNotIn("api_key", payload)
                self.assertEqual(client.get("/api/agents").status_code, 200)
                self.assertEqual(client.patch(f"/api/agents/{payload['id']}", json={"enabled": False}).status_code, 200)
                self.assertEqual(client.delete(f"/api/agents/{payload['id']}").status_code, 200)
        finally:
            main.DB_PATH, main.agent_profiles = old_path, old_service


if __name__ == "__main__":
    unittest.main()
