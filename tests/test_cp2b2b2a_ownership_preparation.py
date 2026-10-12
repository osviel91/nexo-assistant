import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main
from app.safe_migrations import (
    DatabaseLease,
    SafeMigrationEngine,
    SchemaCompatibilityError,
    TRANSACTIONAL_MIGRATIONS,
    _apply_transactional_migration,
    inspect_database,
)
from prototypes.ownership_preparation import SYNTHETIC_APPLICATION_ID, inspect_ownership_preparation
from tests.test_cp2b1_safe_migrations import create_historical_database


def create_v29(path: Path) -> None:
    create_historical_database(path, 28)
    migration = next(item for item in TRANSACTIONAL_MIGRATIONS if item.version == 29)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        with DatabaseLease(path, exclusive=True) as lease:
            _apply_transactional_migration(connection, lease, migration.version, migration.statements,
                                           migration.validate_previous, migration.validate_output)
    finally:
        connection.close()


class OwnershipPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _database(self, name: str, *, version: int = 30, data: bool = False) -> Path:
        directory = self.root / f"build-{name}"
        directory.mkdir()
        source = directory / "nexo.sqlite3"
        if version == 29:
            create_v29(source)
        else:
            SafeMigrationEngine(directory).migrate()
        if data:
            with sqlite3.connect(source) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("INSERT INTO auth_users VALUES('fixture-user','Fixture user','fixture-user',"
                                   "'scrypt$v1$fake','active',1,1)")
                connection.execute("INSERT INTO auth_user_permissions VALUES('fixture-user','chat:read')")
                connection.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) "
                                   "VALUES('bootstrap_completed','fixture-user',2)")
                connection.execute("UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at='2' WHERE id=1")
                connection.execute("INSERT INTO providers VALUES('fixture-provider','Synthetic','https://invalid/','FAKE_SECRET_NOT_REPORT','1')")
                connection.execute("INSERT INTO notebooks(id,name,description,created_at,updated_at,owner_user_id) "
                                   "VALUES('notebook-1','Fixture notebook','','1','2','fixture-user')")
                connection.execute("INSERT INTO conversations(id,title,created_at,updated_at,notebook_id,owner_user_id) "
                                   "VALUES('conversation-1','Fixture conversation','1','2','notebook-1','fixture-user')")
                connection.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) "
                                   "VALUES('message-1','conversation-1','user','synthetic','1')")
                connection.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status) "
                                   "VALUES('run-completed','conversation-1','message-1','1','completed')")
                connection.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status) "
                                   "VALUES('run-active','conversation-1','message-1','2','waiting_approval')")
                connection.execute("INSERT INTO runtime_interactions "
                                   "(id,run_id,conversation_id,kind,payload,created_at,expires_at) "
                                   "VALUES('interaction-1','run-active','conversation-1','input','{}','2','9')")
                connection.execute("""INSERT INTO pending_approvals
                    (id,run_id,conversation_id,message_id,tool_call_id,tool_id,action,tool_fingerprint,
                     frozen_arguments,arguments_sha256,safe_summary,created_at)
                    VALUES('approval-1','run-active','conversation-1','message-1','call-1','tool-1',
                           'mutating','fingerprint','{}','hash','synthetic','2')""")
                connection.execute("CREATE TABLE user_preferences "
                                   "(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT NOT NULL)")
                connection.execute("INSERT INTO user_preferences VALUES('last_chat_model','legacy','1')")
                connection.execute("INSERT INTO auth_user_preferences VALUES('fixture-user','theme','dark','2')")
        with sqlite3.connect(source) as connection:
            connection.execute(f"PRAGMA application_id={SYNTHETIC_APPLICATION_ID}")
        fixture = self.root / f"ownership-{name}.sqlite3"
        shutil.copyfile(source, fixture)
        return fixture

    def test_v30_report_is_read_only_not_activation_ready_and_counts_resources(self):
        database = self._database("readonly", data=True)
        before = database.read_bytes()
        mtime = database.stat().st_mtime_ns
        report = inspect_ownership_preparation(database)
        self.assertEqual(report["schema_version"], 30)
        self.assertEqual(report["ledger"], {"rows": 30, "contiguous": True, "latest": 30})
        self.assertEqual(report["integrity"], {"integrity_check": "ok", "foreign_key_violations": 0})
        self.assertEqual(report["administrative_state"]["state"], "bootstrap_completed")
        self.assertEqual(report["resource_counts"]["conversations"], 1)
        self.assertEqual(report["resource_counts"]["notebooks"], 1)
        self.assertEqual(report["ownership"]["conversations"]["unowned"], 0)
        self.assertEqual(report["ownership"]["notebooks"]["unowned"], 0)
        self.assertEqual(report["operations"]["approvals_by_status"], {"pending": 1})
        self.assertEqual(report["operations"]["interactions_by_status"], {"pending": 1})
        self.assertEqual(report["operations"]["active_runs"], 1)
        self.assertEqual(report["operations"]["runs_by_status"], {"completed": 1, "waiting_approval": 1})
        self.assertEqual(report["global_resource_counts"]["providers"], 1)
        self.assertEqual(report["global_resource_counts"]["user_preferences"], 1)
        self.assertEqual(report["user_preference_counts"], {"legacy_global": 1, "per_user": 1})
        self.assertEqual(report["observations"]["schema_manifest"], "validated")
        self.assertEqual(report["observations"]["direct_owner_columns"], "present")
        self.assertIn("approval_capabilities_pending_or_executing", report["blockers"])
        self.assertIn("interactions_pending", report["blockers"])
        self.assertIn("runs_active_or_waiting", report["blockers"])
        self.assertEqual(report["preparation_status"], "BLOCKED")
        self.assertFalse(report["activation_ready"])
        self.assertIn("relationship_anomalies", report)
        self.assertIn("observations", report)
        self.assertNotIn("fixture-user", repr(report))
        self.assertNotIn("approval-1", repr(report))
        self.assertNotIn("tool-1", repr(report))
        self.assertNotIn("fixture-provider", repr(report))
        self.assertNotIn("FAKE_SECRET_NOT_REPORT", repr(report))
        self.assertNotIn("dark", repr(report))
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(database.stat().st_mtime_ns, mtime)
        self.assertEqual(inspect_database(database).version, 30)
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("SELECT value FROM user_preferences "
                                                 "WHERE key='last_chat_model'").fetchone()[0], "legacy")

    def test_v29_and_unowned_v30_are_reported_as_blocked_without_inference(self):
        v29 = inspect_ownership_preparation(self._database("v29", version=29))
        self.assertEqual(v29["schema_version"], 29)
        self.assertEqual(v29["ownership"]["conversations"], {"supported": False, "total": 0, "unowned": 0})
        self.assertIn("ownership_schema_missing", v29["blockers"])
        self.assertFalse(v29["activation_ready"])

        database = self._database("unowned")
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO conversations(id,title,created_at,updated_at) "
                               "VALUES('legacy-conversation','synthetic','1','2')")
            connection.execute("INSERT INTO notebooks(id,name,created_at,updated_at) "
                               "VALUES('legacy-notebook','synthetic','1','2')")
        report = inspect_ownership_preparation(database)
        self.assertEqual(report["ownership"]["conversations"]["unowned"], 1)
        self.assertEqual(report["ownership"]["notebooks"]["unowned"], 1)
        no_admin = inspect_ownership_preparation(self._database("no-admin"))
        self.assertEqual(no_admin["administrative_state"]["active_users"], 0)
        self.assertEqual(no_admin["administrative_state"]["state"], "schema_installed")
        self.assertIn("bootstrap_or_active_user_missing", no_admin["blockers"])
        self.assertIn("unowned_direct_resources_remain", report["blockers"])
        self.assertEqual(inspect_database(database).version, 30)

    def test_relationship_anomalies_and_structural_ambiguities_are_blockers(self):
        database = self._database("relationships")
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO conversations(id,title,created_at,updated_at,notebook_id) "
                               "VALUES('conversation-orphan','synthetic','1','2','missing-notebook')")
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) "
                               "VALUES('message-orphan','conversation-orphan','user','synthetic','1')")
            connection.execute("INSERT INTO message_citations "
                               "(id,message_id,citation_key,notebook_id,source_id,document_id,chunk_id,canonical_start,canonical_end) "
                               "VALUES('citation-1','message-orphan','citation-1','missing-notebook','missing-source',"
                               "'missing-document','missing-chunk',0,0)")
            connection.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status) "
                               "VALUES('run-orphan','conversation-orphan','missing-message','1','started')")
            connection.execute("INSERT INTO runtime_trace_events "
                               "(id,conversation_id,message_id,sequence,type,started_at,status) "
                               "VALUES('trace-1','missing-conversation','missing-message',1,'trace','1','ok')")
            connection.execute("INSERT INTO shadow_observations "
                               "(id,conversation_id,message_id,mode,created_at) "
                               "VALUES('shadow-1','missing-conversation','missing-message','chat','1')")
        report = inspect_ownership_preparation(database)
        self.assertEqual(report["relationship_anomalies"]["conversation_notebook_missing"], 1)
        self.assertEqual(report["relationship_anomalies"]["run_message_missing"], 1)
        self.assertEqual(report["relationship_anomalies"]["citation_references_missing"], 1)
        self.assertEqual(report["relationship_anomalies"]["legacy_trace_references_missing"], 1)
        self.assertEqual(report["relationship_anomalies"]["shadow_references_missing"], 1)
        self.assertIn("resource_relationship_anomalies", report["blockers"])
        self.assertIn("unresolved_non_foreign_key_relationships", report["blockers"])

    def test_invalid_schema_ledger_and_paths_fail_closed_without_creating_files(self):
        database = self._database("invalid")
        with sqlite3.connect(database) as connection:
            connection.execute("DROP INDEX idx_conversations_owner_updated")
        with self.assertRaises(SchemaCompatibilityError):
            inspect_ownership_preparation(database)

        future = self._database("future-ledger")
        with sqlite3.connect(future) as connection:
            connection.execute("INSERT INTO schema_migrations VALUES(31,'future')")
        with self.assertRaises(SchemaCompatibilityError):
            inspect_ownership_preparation(future)

        missing = self.root / "must-not-be-created.sqlite3"
        with self.assertRaises(ValueError):
            inspect_ownership_preparation(missing)
        self.assertFalse(missing.exists())
        unmarked = self._database("unmarked")
        with sqlite3.connect(unmarked) as connection:
            connection.execute("PRAGMA application_id=0")
        with self.assertRaises(ValueError):
            inspect_ownership_preparation(unmarked)
        with self.assertRaises(ValueError):
            inspect_ownership_preparation(self.root / "nexo.sqlite3")
        symlink = self.root / "ownership-link.sqlite3"
        symlink.symlink_to(database)
        with self.assertRaises(ValueError):
            inspect_ownership_preparation(symlink)
        configured_data = self.root / "configured-data"
        configured_data.mkdir()
        configured_fixture = configured_data / "copied.sqlite3"
        shutil.copyfile(database, configured_fixture)
        with patch.dict(os.environ, {"NEXO_DATA_DIR": str(configured_data)}):
            with self.assertRaises(ValueError):
                inspect_ownership_preparation(configured_fixture)
        with self.assertRaises(ValueError):
            inspect_ownership_preparation(Path.cwd() / "not-a-database.sqlite3")

    def test_verifier_does_not_add_auth_routes_or_claim_authorization(self):
        database = self._database("routes")
        report = inspect_ownership_preparation(database)
        self.assertFalse(report["activation_ready"])
        self.assertIn("runtime_ownership_enforcement_not_implemented", report["blockers"])
        self.assertFalse(any(route.path.startswith("/api/auth") for route in main.app.routes))


if __name__ == "__main__":
    unittest.main()
