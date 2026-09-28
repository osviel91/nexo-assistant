import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.agent_profiles import AgentProfileInput, AgentProfileRepository, AgentProfileService
from app.migrations import migrate


class Stage6CConversationBindingTests(unittest.TestCase):
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
            db.execute("INSERT INTO providers VALUES ('p', 'Provider', 'http://provider', '', 'now')")
            db.execute("INSERT INTO models(id,provider_id,label) VALUES ('m','p','Model')")
            db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('c','Chat','now','now')")
        self.service = AgentProfileService(AgentProfileRepository(connection, lambda: "now"), lambda: [])

    def tearDown(self):
        self.directory.cleanup()

    def test_existing_conversation_is_unbound_and_profile_delete_returns_it_to_nexo(self):
        with self.connection() as db:
            self.assertIsNone(db.execute("SELECT agent_profile_id FROM conversations WHERE id='c'").fetchone()[0])
        profile = self.service.create(AgentProfileInput("Research", provider_id="p", model_id="m"))
        with self.connection() as db:
            db.execute("UPDATE conversations SET agent_profile_id=? WHERE id='c'", (profile["id"],))
        self.service.delete(profile["id"])
        with self.connection() as db:
            self.assertIsNone(db.execute("SELECT agent_profile_id FROM conversations WHERE id='c'").fetchone()[0])
            self.assertIsNotNone(db.execute("SELECT id FROM conversations WHERE id='c'").fetchone())

    def test_chat_request_distinguishes_inherit_from_explicit_clear(self):
        from app.main import ChatIn

        inherited = ChatIn(provider_id="p", model_id="m", content="hello")
        cleared = ChatIn(provider_id="p", model_id="m", content="hello", agent_profile_id=None)
        self.assertNotIn("agent_profile_id", inherited.model_fields_set)
        self.assertIn("agent_profile_id", cleared.model_fields_set)


if __name__ == "__main__":
    unittest.main()
