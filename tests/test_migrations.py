import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.migrations import migrate


class MigrationTests(unittest.TestCase):
    def test_fresh_database_is_created_and_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fresh.sqlite3"
            with sqlite3.connect(path) as connection:
                migrate(connection)
                migrate(connection)
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                version_count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertIn("providers", tables)
            self.assertIn("runtime_trace_events", tables)
            self.assertIn("runtime_runs", tables)
            self.assertIn("runtime_events", tables)
            self.assertEqual(version_count, 9)
            self.assertIn("agent_profiles", tables)
            self.assertIn("agent_profile_tools", tables)
            self.assertIn("agent_profile_id", {row[1] for row in connection.execute("PRAGMA table_info(conversations)")})
            self.assertIn("notebook_id", {row[1] for row in connection.execute("PRAGMA table_info(conversations)")})
            self.assertIn("message_citations", tables)

    def test_existing_database_gets_missing_columns_without_replacing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
                CREATE TABLE models (id TEXT NOT NULL, provider_id TEXT NOT NULL, label TEXT NOT NULL, PRIMARY KEY(provider_id, id));
                CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, provider_id TEXT, model_id TEXT, attachments TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL);
                INSERT INTO providers VALUES ('p', 'Existing', 'http://provider', '', 'now');
                INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('c', 'Conversation', 'now', 'now');
                INSERT INTO messages VALUES ('m', 'c', 'user', 'keep', NULL, NULL, '[]', 'now');
                """)
                migrate(connection)
                migrate(connection)
                model_columns = {row[1] for row in connection.execute("PRAGMA table_info(models)")}
                message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
                provider = connection.execute("SELECT name FROM providers WHERE id='p'").fetchone()
                conversation = connection.execute("SELECT title FROM conversations WHERE id='c'").fetchone()
                message = connection.execute("SELECT content FROM messages WHERE id='m'").fetchone()
            self.assertIn("capabilities", model_columns)
            self.assertIn("sources", message_columns)
            self.assertEqual(provider[0], "Existing")
            self.assertEqual(conversation[0], "Conversation")
            self.assertEqual(message[0], "keep")
