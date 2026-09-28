import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.agent_profiles import AgentProfileInput, AgentProfileRepository, AgentProfileService
from app.migrations import migrate


class Stage8AValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nexo.sqlite3"

        def connection():
            result = sqlite3.connect(self.path)
            result.row_factory = sqlite3.Row
            result.execute("PRAGMA foreign_keys=ON")
            return result

        self.connection = connection
        with connection() as database:
            migrate(database)
            database.execute("INSERT INTO providers VALUES ('p', 'Provider', 'http://provider', '', 'now')")
            database.execute("INSERT INTO models(id,provider_id,label) VALUES ('m','p','Model')")
            database.execute("INSERT INTO conversations(id,title,created_at,updated_at,notebook_id) VALUES ('c','Original','old','old','n')")
            database.execute("INSERT INTO notebooks(id,name,description,created_at,updated_at) VALUES ('n','Knowledge','','now','now')")
            database.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) VALUES ('message','c','assistant','historical','now')")
        self.service = AgentProfileService(AgentProfileRepository(connection, lambda: "now"), lambda: [])

    def tearDown(self):
        self.directory.cleanup()

    def test_mode_transitions_preserve_history_and_notebook(self):
        from app import main

        old_db, old_profiles = main.DB_PATH, main.agent_profiles
        main.DB_PATH = self.path
        main.agent_profiles = self.service
        try:
            profile = self.service.create(AgentProfileInput("Research", provider_id="p", model_id="m", system_instructions="Soul"))
            agent = main.update_conversation("c", main.ConversationPatch(execution_mode="agent", agent_profile_id=profile["id"]))
            self.assertEqual((agent["execution_mode"], agent["agent_profile_id"], agent["notebook_id"]), ("agent", profile["id"], "n"))
            chat = main.update_conversation("c", main.ConversationPatch(execution_mode="chat"))
            self.assertEqual((chat["execution_mode"], chat["agent_profile_id"], chat["notebook_id"]), ("chat", None, "n"))
            with main.db() as database:
                self.assertEqual(database.execute("SELECT content FROM messages WHERE id='message'").fetchone()[0], "historical")
                self.assertIsNotNone(database.execute("SELECT id FROM notebooks WHERE id='n'").fetchone())
        finally:
            main.DB_PATH, main.agent_profiles = old_db, old_profiles

    def test_delete_conversation_does_not_delete_agent_or_notebook(self):
        from app import main

        old_db, old_profiles = main.DB_PATH, main.agent_profiles
        main.DB_PATH = self.path
        main.agent_profiles = self.service
        try:
            profile = self.service.create(AgentProfileInput("Research", provider_id="p", model_id="m"))
            with main.db() as database:
                database.execute("UPDATE conversations SET agent_profile_id=?, execution_mode='agent' WHERE id='c'", (profile["id"],))
                database.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status,metadata) VALUES ('run','c','message','now','completed','{}')")
            self.assertEqual(main.delete_conversation("c"), {"ok": True})
            with main.db() as database:
                self.assertIsNone(database.execute("SELECT id FROM conversations WHERE id='c'").fetchone())
                self.assertIsNone(database.execute("SELECT id FROM messages WHERE id='message'").fetchone())
                self.assertIsNone(database.execute("SELECT id FROM runtime_runs WHERE id='run'").fetchone())
                self.assertIsNotNone(database.execute("SELECT id FROM agent_profiles WHERE id=?", (profile["id"],)).fetchone())
                self.assertIsNotNone(database.execute("SELECT id FROM notebooks WHERE id='n'").fetchone())
        finally:
            main.DB_PATH, main.agent_profiles = old_db, old_profiles

    def test_agent_mode_requires_profile_and_chat_cannot_apply_one(self):
        from app import main

        old_db = main.DB_PATH
        main.DB_PATH = self.path
        try:
            with self.assertRaisesRegex(Exception, "agent_profile_required"):
                main.update_conversation("c", main.ConversationPatch(execution_mode="agent"))

            with self.connection() as database:
                database.execute("UPDATE conversations SET agent_profile_id='legacy', execution_mode='agent' WHERE id='c'")
            with self.assertRaisesRegex(Exception, "agent_profile_not_found"):
                main.update_conversation("c", main.ConversationPatch(execution_mode="chat", agent_profile_id="legacy"))
        finally:
            main.DB_PATH = old_db


if __name__ == "__main__":
    unittest.main()
