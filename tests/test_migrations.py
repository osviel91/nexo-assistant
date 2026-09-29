import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.migrations import MIGRATIONS, migrate


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
            self.assertEqual(version_count, 15)
            self.assertIn("agent_profiles", tables)
            self.assertIn("agent_profile_tools", tables)
            self.assertIn("agent_profile_id", {row[1] for row in connection.execute("PRAGMA table_info(conversations)")})
            self.assertIn("notebook_id", {row[1] for row in connection.execute("PRAGMA table_info(conversations)")})
            self.assertIn("execution_mode", {row[1] for row in connection.execute("PRAGMA table_info(conversations)")})
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

    def test_execution_mode_migrates_legacy_bindings_without_mutating_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
                CREATE TABLE models (id TEXT NOT NULL, provider_id TEXT NOT NULL, label TEXT NOT NULL, PRIMARY KEY(provider_id, id));
                CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                INSERT INTO providers VALUES ('p', 'Existing', 'http://provider', '', 'now');
                INSERT INTO conversations VALUES ('agent', 'Agent chat', 'created', 'updated');
                INSERT INTO conversations VALUES ('chat', 'Raw chat', 'created', 'updated');
                """)
                connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                for version, migration in MIGRATIONS[:12]:
                    migration(connection)
                    connection.execute("INSERT INTO schema_migrations VALUES (?, 'now')", (version,))
                connection.execute("UPDATE conversations SET agent_profile_id='profile' WHERE id='agent'")
                connection.commit()
                migrate(connection)
                rows = {row[0]: row for row in connection.execute("SELECT id, execution_mode, title FROM conversations")}
                version_count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(rows["agent"][1], "agent")
            self.assertEqual(rows["chat"][1], "chat")
            self.assertEqual(rows["agent"][2], "Agent chat")
            self.assertEqual(version_count, 15)

    def test_stage9a_migration_backfills_existing_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stage8.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                for version, migration in MIGRATIONS[:11]:
                    migration(connection)
                    connection.execute("INSERT INTO schema_migrations VALUES (?, 'now')", (version,))
                connection.execute("INSERT INTO notebooks VALUES ('n','N','','now','now')")
                connection.execute("INSERT INTO notebook_sources(id,notebook_id,type,title,status,metadata,content_hash,created_at,updated_at) VALUES ('s','n','file','S','ready','{}','hash','now','now')")
                connection.execute("INSERT INTO canonical_documents VALUES ('d','n','s','S','rare lexical phrase','hash','text',NULL,'{}','now','now')")
                connection.execute("INSERT INTO document_chunks(id,notebook_id,document_id,source_id,ordinal,content,canonical_start,canonical_end,token_count,metadata,content_hash,created_at) VALUES ('c','n','d','s',0,'rare lexical phrase',0,19,3,'{}','chunk','now')")
                migrate(connection)
                row = connection.execute("SELECT chunk_id, notebook_id, source_id, content FROM document_chunks_fts").fetchone()
            self.assertEqual(row, ("c", "n", "s", "rare lexical phrase"))

    def test_fts_cleanup_removes_orphans_and_tracks_chunk_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fts.sqlite3"
            with sqlite3.connect(path) as connection:
                migrate(connection)
                connection.execute("INSERT INTO notebooks VALUES ('n','N','','now','now')")
                connection.execute("INSERT INTO notebook_sources(id,notebook_id,type,title,status,metadata,content_hash,created_at,updated_at) VALUES ('s','n','file','S','ready','{}','hash','now','now')")
                connection.execute("INSERT INTO canonical_documents VALUES ('d','n','s','S','text','hash','text',NULL,'{}','now','now')")
                connection.execute("INSERT INTO document_chunks(id,notebook_id,document_id,source_id,ordinal,content,canonical_start,canonical_end,token_count,metadata,content_hash,created_at) VALUES ('c','n','d','s',0,'text',0,4,1,'{}','chunk','now')")
                connection.execute("INSERT INTO document_chunks_fts VALUES ('orphan','n','s','orphan')")
                connection.execute("DELETE FROM document_chunks WHERE id='c'")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_chunks_fts").fetchone()[0], 1)
                connection.execute("INSERT INTO document_chunks(id,notebook_id,document_id,source_id,ordinal,content,canonical_start,canonical_end,token_count,metadata,content_hash,created_at) VALUES ('c','n','d','s',0,'text',0,4,1,'{}','chunk','now')")
                connection.execute("INSERT INTO document_chunks_fts VALUES ('c','n','s','text')")
                connection.execute("DELETE FROM document_chunks_fts WHERE chunk_id='orphan'")
                connection.execute("DELETE FROM document_chunks WHERE id='c'")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_chunks_fts WHERE chunk_id='c'").fetchone()[0], 0)

    def test_index_metadata_is_not_available_without_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale-index.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                for version, migration in MIGRATIONS[:14]:
                    migration(connection)
                    connection.execute("INSERT INTO schema_migrations VALUES (?, 'now')", (version,))
                connection.executescript("""
                    INSERT INTO notebooks VALUES ('n','N','','now','now');
                    INSERT INTO notebook_sources(id,notebook_id,type,title,status,metadata,content_hash,created_at,updated_at,indexing_status,chunk_count,vector_count) VALUES ('s','n','file','S','ready','{}','hash','now','now','ready',94,94);
                    INSERT INTO canonical_documents VALUES ('d','n','s','S','text','hash','text',NULL,'{}','now','now');
                    INSERT INTO vector_index_identities VALUES ('d','provider','model',2,1,'chunking','hash','now','now');
                """)
                migrate(connection)
                version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
                source = connection.execute("SELECT indexing_status,chunk_count,vector_count FROM notebook_sources WHERE id='s'").fetchone()
                identity = connection.execute("SELECT COUNT(*) FROM vector_index_identities WHERE document_id='d'").fetchone()[0]
            self.assertEqual(version, 15)
            self.assertEqual(tuple(source), ("not_indexed", None, None))
            self.assertEqual(identity, 0)
