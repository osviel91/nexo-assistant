import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.safe_migrations import (
    DatabaseInUse,
    DatabaseLease,
    MigrationRecoveryRequired,
    SafeMigrationEngine,
    SchemaCompatibilityError,
    TRANSACTIONAL_MIGRATIONS,
    _apply_transactional_migration,
    expected_ownership_schema_manifest,
    inspect_database,
    validate_schema_manifest,
    validate_runtime_database,
)
from prototypes.admin_persistence import backup_database, restore_database
from tests.test_cp2b1_safe_migrations import create_historical_database


def create_v29(path: Path, *, preferences: bool = False) -> None:
    create_historical_database(path, 28, preferences=preferences)
    spec = next(item for item in TRANSACTIONAL_MIGRATIONS if item.version == 29)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        with DatabaseLease(path, exclusive=True) as lease:
            _apply_transactional_migration(connection, lease, spec.version, spec.statements,
                                           spec.validate_previous, spec.validate_output)
    finally:
        connection.close()


def wait_ready(process: subprocess.Popen[str]) -> None:
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        if not selector.select(20) or process.stdout.readline().strip() != "READY":
            raise AssertionError("migration process did not reach the barrier")
    finally:
        selector.close()


class OwnershipSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _v29_with_data(self, database: Path, *, preferences: bool = True) -> None:
        create_v29(database, preferences=preferences)
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("INSERT INTO notebooks VALUES('notebook-1','Notebook','fixture','1','2')")
            connection.execute("INSERT INTO conversations(id,title,created_at,updated_at,notebook_id) "
                               "VALUES('conversation-1','Conversation','1','2','notebook-1')")
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) "
                               "VALUES('message-1','conversation-1','user','synthetic','1')")
            connection.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status) "
                               "VALUES('run-1','conversation-1','message-1','1','completed')")
            connection.execute("INSERT INTO runtime_events(id,run_id,sequence,kind,name,started_at,status) "
                               "VALUES('event-1','run-1',1,'step','synthetic','1','completed')")
            connection.execute("INSERT INTO notebook_sources(id,notebook_id,type,title,status,created_at,updated_at) "
                               "VALUES('source-1','notebook-1','file','Source','ready','1','2')")
            connection.execute("INSERT INTO canonical_documents "
                               "(id,notebook_id,source_id,title,content,content_hash,content_type,created_at,updated_at) "
                               "VALUES('document-1','notebook-1','source-1','Document','content','hash','text/plain','1','2')")
            connection.execute("INSERT INTO canonical_spans "
                               "(id,document_id,start_offset,end_offset,source_type,source_location) "
                               "VALUES('span-1','document-1',0,7,'file','{}')")
            connection.execute("INSERT INTO document_chunks "
                               "(id,notebook_id,document_id,source_id,ordinal,content,canonical_start,canonical_end,content_hash,created_at) "
                               "VALUES('chunk-1','notebook-1','document-1','source-1',0,'content',0,7,'chunk-hash','1')")
            if preferences:
                connection.execute("UPDATE user_preferences SET value='fixture-model' "
                                   "WHERE key='last_chat_model'")

    def test_v29_to_v30_preserves_rows_and_leaves_existing_resources_unowned(self):
        data_dir = self.root / "upgrade"
        data_dir.mkdir()
        database = data_dir / "nexo.sqlite3"
        self._v29_with_data(database)
        with sqlite3.connect(database) as connection:
            before = {table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                      for table in ("conversations", "notebooks", "messages", "runtime_runs", "runtime_events",
                                    "notebook_sources", "canonical_documents", "canonical_spans", "document_chunks",
                                    "user_preferences")}

        result = SafeMigrationEngine(data_dir).migrate()
        self.assertEqual((result.source_version, result.target_version), (29, 30))
        self.assertEqual(validate_runtime_database(database).version, 30)
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            for table, rows in before.items():
                columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                old_columns = columns[:-1] if table in {"conversations", "notebooks"} else columns
                self.assertEqual(connection.execute(
                    f"SELECT {','.join(old_columns)} FROM {table} ORDER BY rowid").fetchall(), rows)
            self.assertIsNone(connection.execute(
                "SELECT owner_user_id FROM conversations WHERE id='conversation-1'").fetchone()[0])
            self.assertIsNone(connection.execute(
                "SELECT owner_user_id FROM notebooks WHERE id='notebook-1'").fetchone()[0])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_user_preferences").fetchone()[0], 0)
            self.assertEqual(tuple(row[0] for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version")), tuple(range(1, 31)))
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_nullable_owners_enforce_valid_user_references_and_preferences_are_isolated(self):
        data_dir = self.root / "constraints"
        data_dir.mkdir()
        database = data_dir / "nexo.sqlite3"
        self._v29_with_data(database)
        SafeMigrationEngine(data_dir).migrate()
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("INSERT INTO auth_users VALUES('u1','User 1','user-1','scrypt$v1$fake','active',1,1)")
            connection.execute("INSERT INTO auth_users VALUES('u2','User 2','user-2','scrypt$v1$fake','active',1,1)")
            connection.execute("INSERT INTO auth_user_permissions VALUES('u1','chat:read')")
            connection.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) "
                               "VALUES('bootstrap_completed','u1',2)")
            connection.execute("UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at='2' WHERE id=1")
            connection.execute("UPDATE conversations SET owner_user_id='u1' WHERE id='conversation-1'")
            connection.execute("UPDATE notebooks SET owner_user_id='u2' WHERE id='notebook-1'")
            connection.execute("INSERT INTO auth_user_preferences VALUES('u1','theme','dark','2')")
            connection.execute("INSERT INTO auth_user_preferences VALUES('u2','theme','light','2')")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_user_preferences VALUES('u1','theme','other','3')")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_user_preferences VALUES('missing','theme','x','3')")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE conversations SET owner_user_id='missing' WHERE id='conversation-1'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE notebooks SET owner_user_id='missing' WHERE id='notebook-1'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM auth_users WHERE id='u1'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM auth_users WHERE id='u2'")
            self.assertEqual(connection.execute("SELECT value FROM auth_user_preferences "
                                                 "WHERE user_id='u1' AND key='theme'").fetchone()[0], "dark")
            self.assertEqual(connection.execute("SELECT value FROM auth_user_preferences "
                                                 "WHERE user_id='u2' AND key='theme'").fetchone()[0], "light")
            self.assertEqual(connection.execute("SELECT value FROM user_preferences "
                                                 "WHERE key='last_chat_model'").fetchone()[0], "fixture-model")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(inspect_database(database).version, 30)

    def test_v30_manifest_captures_owner_columns_foreign_keys_indexes_and_constraints(self):
        data_dir = self.root / "manifest"
        data_dir.mkdir()
        database = data_dir / "nexo.sqlite3"
        SafeMigrationEngine(data_dir).migrate()
        expected = expected_ownership_schema_manifest()
        tables = {table.name: table for table in expected.tables}
        for table_name in ("conversations", "notebooks"):
            table = tables[table_name]
            owner = next(column for column in table.columns if column[0] == "owner_user_id")
            self.assertEqual(owner[1], "TEXT")
            self.assertEqual(owner[2], 0)
            fk = next(foreign_key for foreign_key in table.foreign_keys if foreign_key[3] == "owner_user_id")
            self.assertEqual((fk[2], fk[4], fk[6]), ("auth_users", "id", "RESTRICT"))
        user_preferences = tables["auth_user_preferences"]
        self.assertEqual([column[0] for column in user_preferences.columns if column[4]], ["user_id", "key"])
        self.assertIn("CHECK", next(item.sql for item in expected.objects
                                    if item.kind == "table" and item.name == "auth_user_preferences"))
        self.assertIn("idx_conversations_owner_updated", {item.name for item in expected.objects})
        self.assertIn("idx_notebooks_owner_updated", {item.name for item in expected.objects})

        with sqlite3.connect(database) as connection:
            validate_schema_manifest(connection, expected)
            for table_name in ("conversations", "notebooks"):
                owner_fk = next(row for row in connection.execute(
                    f"PRAGMA foreign_key_list({table_name})") if row[3] == "owner_user_id")
                self.assertEqual((owner_fk[2], owner_fk[4], owner_fk[6]),
                                 ("auth_users", "id", "RESTRICT"))
            conversation = tables["conversations"]
            missing_column = replace(expected, tables=tuple(
                replace(table, columns=tuple(column for column in table.columns
                                             if not (table.name == "conversations" and column[0] == "owner_user_id")))
                if table.name == "conversations" else table for table in expected.tables))
            with self.assertRaises(SchemaCompatibilityError):
                validate_schema_manifest(connection, missing_column)

            changed_fk_rows = tuple(tuple((*row[:6], "CASCADE", *row[7:]))
                                    if row[3] == "owner_user_id" else row
                                    for row in conversation.foreign_keys)
            changed_fk = replace(expected, tables=tuple(
                replace(table, foreign_keys=changed_fk_rows) if table.name == "conversations" else table
                for table in expected.tables))
            with self.assertRaises(SchemaCompatibilityError):
                validate_schema_manifest(connection, changed_fk)

            changed_constraint = replace(expected, objects=tuple(
                replace(item, sql=(item.sql or "") + " altered")
                if item.kind == "table" and item.name == "auth_user_preferences" else item
                for item in expected.objects))
            with self.assertRaises(SchemaCompatibilityError):
                validate_schema_manifest(connection, changed_constraint)

            owner_index = "idx_conversations_owner_updated"
            missing_index = replace(expected,
                objects=tuple(item for item in expected.objects if item.name != owner_index),
                tables=tuple(replace(table, indexes=tuple(index for index in table.indexes
                                                          if index[0] != owner_index))
                             if table.name == "conversations" else table for table in expected.tables))
            with self.assertRaises(SchemaCompatibilityError):
                validate_schema_manifest(connection, missing_index)

            changed_index = replace(expected, objects=tuple(
                replace(item, sql=(item.sql or "") + " altered")
                if item.kind == "index" and item.name == owner_index else item
                for item in expected.objects))
            with self.assertRaises(SchemaCompatibilityError):
                validate_schema_manifest(connection, changed_index)

    def test_runtime_manifest_rejects_renamed_owner_column_and_weakened_preference_constraint(self):
        for name, mutation in (
            ("renamed-column", "ALTER TABLE conversations RENAME COLUMN owner_user_id TO owner_id"),
            ("weakened-check", None),
        ):
            with self.subTest(name=name):
                data_dir = self.root / name
                data_dir.mkdir()
                database = data_dir / "nexo.sqlite3"
                SafeMigrationEngine(data_dir).migrate()
                with sqlite3.connect(database) as connection:
                    if mutation:
                        connection.execute(mutation)
                    else:
                        connection.execute("PRAGMA foreign_keys=OFF")
                        connection.execute("ALTER TABLE auth_user_preferences RENAME TO auth_user_preferences_old")
                        connection.execute("CREATE TABLE auth_user_preferences (user_id TEXT NOT NULL "
                                           "REFERENCES auth_users(id) ON DELETE CASCADE, key TEXT NOT NULL, "
                                           "value TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(user_id,key))")
                        connection.execute("DROP TABLE auth_user_preferences_old")
                with self.assertRaises(SchemaCompatibilityError):
                    inspect_database(database)

    def test_legacy_preferences_endpoints_and_schema_remain_unchanged_after_v30(self):
        data_dir = self.root / "legacy-preferences"
        data_dir.mkdir()
        database = data_dir / "nexo.sqlite3"
        create_historical_database(database, 28, preferences=True)
        with sqlite3.connect(database) as connection:
            legacy_definition = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_preferences'").fetchone()[0]
            legacy_columns = connection.execute("PRAGMA table_info(user_preferences)").fetchall()
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE user_preferences SET value='before' WHERE key='last_chat_model'")
        SafeMigrationEngine(data_dir).migrate()
        previous_db = main.DB_PATH
        main.DB_PATH = database
        try:
            with TestClient(main.app) as client:
                self.assertEqual(client.get("/api/preferences").json(), {"last_chat_model": "before"})
                response = client.patch("/api/preferences", json={"last_agent_profile": "fixture-profile"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"last_chat_model": "before",
                                                   "last_agent_profile": "fixture-profile"})
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_preferences'").fetchone()[0],
                    legacy_definition)
                self.assertEqual(connection.execute("PRAGMA table_info(user_preferences)").fetchall(), legacy_columns)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_user_preferences").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT value FROM user_preferences "
                                                     "WHERE key='last_agent_profile'").fetchone()[0], "fixture-profile")
        finally:
            main.shutdown()
            main.DB_PATH = previous_db

    def test_v30_partial_schema_and_future_ledger_fail_closed(self):
        mutations = (
            ("missing-index", ("DROP INDEX idx_conversations_owner_updated",)),
            ("changed-index", ("DROP INDEX idx_conversations_owner_updated",
                               "CREATE INDEX idx_conversations_owner_updated ON conversations(updated_at)")),
            ("missing-table", ("DROP TABLE auth_user_preferences",)),
            ("future-ledger", ("INSERT INTO schema_migrations VALUES(31,'future')",)),
        )
        for name, statements in mutations:
            with self.subTest(name=name):
                data_dir = self.root / str(len(list(self.root.iterdir())))
                data_dir.mkdir()
                database = data_dir / "nexo.sqlite3"
                SafeMigrationEngine(data_dir).migrate()
                with sqlite3.connect(database) as connection:
                    for statement in statements:
                        connection.execute(statement)
                with self.assertRaises(SchemaCompatibilityError):
                    inspect_database(database)

    def test_sql_failure_rolls_back_v30_then_retries(self):
        data_dir = self.root / "sql-failure"
        data_dir.mkdir()
        database = data_dir / "nexo.sqlite3"
        create_v29(database)
        original = database.read_bytes()

        def fail_after_first_owner_column(stage):
            if stage == "after_statement:1":
                raise RuntimeError("injected v30 statement failure")

        with self.assertRaisesRegex(RuntimeError, "injected v30"):
            SafeMigrationEngine(data_dir).migrate(fault_hook=fail_after_first_owner_column)
        self.assertEqual(database.read_bytes(), original)
        self.assertEqual(inspect_database(database).version, 29)
        self.assertEqual(SafeMigrationEngine(data_dir).migrate().target_version, 30)

    def test_sigkill_around_v30_commit_reopens_with_atomic_schema_data_and_ledger(self):
        for barrier, expected_version in (("after_ledger", 29), ("after_commit", 30)):
            with self.subTest(barrier=barrier):
                data_dir = self.root / barrier
                data_dir.mkdir()
                database = data_dir / "nexo.sqlite3"
                self._v29_with_data(database)
                code = """import sys
from app.safe_migrations import SafeMigrationEngine
def stop(stage):
    if stage == sys.argv[2]:
        print('READY', flush=True)
        while True: pass
SafeMigrationEngine(sys.argv[1]).migrate(fault_hook=stop)
"""
                process = subprocess.Popen([sys.executable, "-c", code, str(data_dir), barrier],
                                           stdout=subprocess.PIPE, text=True)
                wait_ready(process)
                process.send_signal(signal.SIGKILL)
                self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
                stage = next(data_dir.glob(".nexo.sqlite3.stage.*.sqlite3"))
                expected = inspect_database(stage)
                self.assertEqual(expected.version, expected_version)
                with sqlite3.connect(stage) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM notebook_sources").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM canonical_documents").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0], 1)
                    self.assertEqual(tuple(row[0] for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version")),
                        tuple(range(1, expected_version + 1)))
                    if expected_version == 29:
                        self.assertNotIn("owner_user_id", {row[1] for row in connection.execute(
                            "PRAGMA table_info(conversations)")})
                    else:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM conversations "
                                                            "WHERE owner_user_id IS NOT NULL").fetchone()[0], 0)
                        self.assertIsNone(connection.execute("SELECT owner_user_id FROM conversations "
                                                             "WHERE id='conversation-1'").fetchone()[0])
                        self.assertEqual(connection.execute("SELECT value FROM user_preferences "
                                                             "WHERE key='last_chat_model'").fetchone()[0],
                                         "fixture-model")
                result = SafeMigrationEngine(data_dir).migrate()
                self.assertEqual(result.target_version, 30)
                self.assertEqual(inspect_database(database).version, 30)
                with sqlite3.connect(database) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(connection.execute("SELECT value FROM user_preferences "
                                                        "WHERE key='last_chat_model'").fetchone()[0], "fixture-model")
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_user_preferences").fetchone()[0], 0)

    def test_two_concurrent_migrators_are_serialized(self):
        data_dir = self.root / "concurrent"
        data_dir.mkdir()
        database = data_dir / "nexo.sqlite3"
        create_v29(database)
        code = """import sys
from app.safe_migrations import SafeMigrationEngine
def pause(stage):
    if stage == 'after_backup':
        print('READY', flush=True)
        sys.stdin.read(1)
SafeMigrationEngine(sys.argv[1]).migrate(fault_hook=pause)
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(data_dir)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        try:
            wait_ready(process)
            with self.assertRaises(DatabaseInUse):
                SafeMigrationEngine(data_dir).migrate()
            process.stdin.write("continue\n")
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=30), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        self.assertEqual(inspect_database(database).version, 30)

    def test_sigkill_before_and_after_promotion_recovers_v30_with_all_rows_and_ledger(self):
        for barrier, promoted in (("before_replace", False), ("after_replace", True)):
            with self.subTest(barrier=barrier):
                data_dir = self.root / barrier
                data_dir.mkdir()
                database = data_dir / "nexo.sqlite3"
                self._v29_with_data(database)
                code = """import sys
from app.safe_migrations import SafeMigrationEngine
def stop(stage):
    if stage == sys.argv[2]:
        print('READY', flush=True)
        while True: pass
SafeMigrationEngine(sys.argv[1]).migrate(fault_hook=stop)
"""
                process = subprocess.Popen([sys.executable, "-c", code, str(data_dir), barrier],
                                           stdout=subprocess.PIPE, text=True)
                wait_ready(process)
                process.send_signal(signal.SIGKILL)
                self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
                self.assertTrue((data_dir / ".nexo.sqlite3.promotion.json").exists())
                with self.assertRaises(MigrationRecoveryRequired):
                    validate_runtime_database(database)
                self.assertEqual(inspect_database(database).version, 30 if promoted else 29)

                result = SafeMigrationEngine(data_dir).migrate()
                self.assertEqual((result.target_version, result.changed), (30, promoted is False))
                self.assertFalse((data_dir / ".nexo.sqlite3.promotion.json").exists())
                self.assertEqual(inspect_database(database).version, 30)
                with sqlite3.connect(database) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(tuple(row[0] for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version")), tuple(range(1, 31)))
                    for table in ("conversations", "notebooks", "messages", "runtime_runs", "runtime_events",
                                  "notebook_sources", "canonical_documents", "canonical_spans", "document_chunks"):
                        self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT owner_user_id FROM conversations "
                                                        "WHERE id='conversation-1'").fetchone()[0], None)
                    self.assertEqual(connection.execute("SELECT owner_user_id FROM notebooks "
                                                        "WHERE id='notebook-1'").fetchone()[0], None)
                    self.assertEqual(connection.execute("SELECT value FROM user_preferences "
                                                        "WHERE key='last_chat_model'").fetchone()[0], "fixture-model")
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_user_preferences").fetchone()[0], 0)

    def test_v30_backup_restore_and_runtime_compatibility_from_v28_v29(self):
        for version in (28, 29):
            data_dir = self.root / f"source-v{version}"
            data_dir.mkdir()
            database = data_dir / "nexo.sqlite3"
            if version == 29:
                self._v29_with_data(database)
            else:
                create_historical_database(database, 28, preferences=True)
            self.assertEqual(validate_runtime_database(database).version, version)
            result = SafeMigrationEngine(data_dir).migrate()
            self.assertEqual(result.target_version, 30)
            backup_input = self.root / f"backup-input-v{version}.sqlite3"
            backup = self.root / f"backup-v{version}.sqlite3"
            restored = self.root / f"restore-v{version}.sqlite3"
            shutil.copyfile(database, backup_input)
            backup_database(backup_input, backup)
            restore_database(backup, restored)
            self.assertEqual(inspect_database(restored).version, 30)
            self.assertEqual(inspect_database(backup).version, 30)


if __name__ == "__main__":
    unittest.main()
