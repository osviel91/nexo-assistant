import json
import os
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import unittest
from unittest.mock import patch

from app.safe_migrations import (DatabaseInUse, SafeMigrationEngine, acquire_runtime_lease,
                                inspect_database)
from prototypes.historical_ownership_assignment import (
    AssignmentRejected,
    FIXTURE_CONFIRMATION,
    SyntheticAssignmentConsent,
    assign_historical_ownership,
    assignment_manifest_sha256,
)
from prototypes.ownership_preparation import SYNTHETIC_APPLICATION_ID


def create_fixture(root: Path, *, run_status="completed", approval_status="pending",
                   interaction_status="pending", legacy_preferences=True) -> tuple[Path, Path]:
    directory = root / f"db-{len(list(root.iterdir()))}"
    directory.mkdir()
    SafeMigrationEngine(directory).migrate()
    database = directory / "nexo.sqlite3"
    fixture = directory / "nexo-fixture.sqlite3"
    database.rename(fixture)
    with sqlite3.connect(fixture) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(f"PRAGMA application_id={SYNTHETIC_APPLICATION_ID}")
        db.execute("INSERT INTO auth_users VALUES('admin-fixture','Fixture Admin','fixture-admin','synthetic-hash','active',1,1)")
        db.execute("INSERT INTO auth_user_permissions VALUES('admin-fixture','chat:read')")
        db.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) "
                   "VALUES('bootstrap_completed','admin-fixture',1)")
        db.execute("UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at='1' WHERE id=1")
        db.execute("INSERT INTO notebooks(id,name,description,created_at,updated_at) "
                   "VALUES('notebook-1','Synthetic notebook','fixture','1','2')")
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at,notebook_id) "
                   "VALUES('conversation-1','Synthetic conversation','1','2','notebook-1')")
        db.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) "
                   "VALUES('message-1','conversation-1','user','synthetic only','1')")
        db.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,completed_at,status) "
                   "VALUES('run-1','conversation-1','message-1','1',?,?)",
                   ("2" if run_status in {"completed", "failed"} else None, run_status))
        db.execute("INSERT INTO runtime_events(id,run_id,sequence,kind,name,started_at,status) "
                   "VALUES('event-1','run-1',1,'step','synthetic','1','completed')")
        db.execute("INSERT INTO notebook_sources(id,notebook_id,type,title,status,created_at,updated_at) "
                   "VALUES('source-1','notebook-1','file','Synthetic source','ready','1','2')")
        db.execute("INSERT INTO canonical_documents "
                   "(id,notebook_id,source_id,title,content,content_hash,content_type,created_at,updated_at) "
                   "VALUES('document-1','notebook-1','source-1','Synthetic doc','fixture text','hash','text/plain','1','2')")
        db.execute("INSERT INTO canonical_spans "
                   "(id,document_id,start_offset,end_offset,source_type,source_location) "
                   "VALUES('span-1','document-1',0,12,'file','{}')")
        db.execute("INSERT INTO document_chunks "
                   "(id,notebook_id,document_id,source_id,ordinal,content,canonical_start,canonical_end,content_hash,created_at) "
                   "VALUES('chunk-1','notebook-1','document-1','source-1',0,'fixture text',0,12,'chunk-hash','1')")
        approval_statuses = ((approval_status,) if isinstance(approval_status, str)
                             else tuple(approval_status or ()))
        for index, status in enumerate(approval_statuses, start=1):
            db.execute("""INSERT INTO pending_approvals
                (id,run_id,conversation_id,message_id,tool_call_id,tool_id,action,tool_fingerprint,
                 frozen_arguments,arguments_sha256,safe_summary,status,created_at)
                VALUES(?,'run-1','conversation-1','message-1',?,'fixture-tool',
                 'mutating','fingerprint','{}','hash','{}',?,'1')""",
                       (f"approval-{index}", f"call-{index}", status))
        interaction_statuses = ((interaction_status,) if isinstance(interaction_status, str)
                                else tuple(interaction_status or ()))
        for index, status in enumerate(interaction_statuses, start=1):
            db.execute("""INSERT INTO runtime_interactions
                (id,run_id,conversation_id,kind,payload,status,created_at,expires_at)
                VALUES(?,'run-1','conversation-1','form','{}',?,'1','9999-01-01T00:00:00+00:00')""",
                       (f"interaction-{index}", status))
        if legacy_preferences:
            db.execute("CREATE TABLE user_preferences(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT NOT NULL)")
            db.execute("INSERT INTO user_preferences VALUES('theme','synthetic-theme','1')")
    backup = directory / "backup-fixture.sqlite3"
    with sqlite3.connect(fixture) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    with sqlite3.connect(backup) as db:
        db.execute(f"PRAGMA application_id={SYNTHETIC_APPLICATION_ID}")
    return fixture, backup


def consent(database: Path, backup: Path, operation_id="op-fixture-1", *,
            confirmation=FIXTURE_CONFIRMATION, target="admin-fixture", manifest_override=None):
    with sqlite3.connect(database) as db:
        conversations = tuple(row[0] for row in db.execute("SELECT id FROM conversations ORDER BY id"))
        notebooks = tuple(row[0] for row in db.execute("SELECT id FROM notebooks ORDER BY id"))
    manifest = {"version": 1, "operation_id": operation_id, "target_user_id": target,
                "conversation_ids": list(conversations), "notebook_ids": list(notebooks)}
    if manifest_override is not None:
        manifest = manifest_override
    return SyntheticAssignmentConsent(confirmation, operation_id, target, conversations, notebooks,
                                      manifest, assignment_manifest_sha256(manifest), 1234.0)


class HistoricalOwnershipAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database, self.backup = create_fixture(self.root)
        self.consent = consent(self.database, self.backup)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_assignment_updates_only_direct_owners_and_safe_pending_records(self):
        before = inspect_database(self.database).data_digest
        result = assign_historical_ownership(self.database, self.backup, self.consent)
        self.assertEqual((result.conversations_assigned, result.notebooks_assigned,
                          result.approvals_invalidated, result.interactions_denied,
                          result.already_committed), (1, 1, 1, 1, False))
        self.assertNotEqual(inspect_database(self.database).data_digest, before)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT owner_user_id FROM conversations").fetchone()[0], "admin-fixture")
            self.assertEqual(db.execute("SELECT owner_user_id FROM notebooks").fetchone()[0], "admin-fixture")
            self.assertEqual(db.execute("SELECT content FROM messages").fetchone()[0], "synthetic only")
            self.assertEqual(db.execute("SELECT status FROM pending_approvals").fetchone()[0], "invalidated")
            self.assertEqual(db.execute("SELECT status,response FROM runtime_interactions").fetchone(),
                             ("denied", '{"approved":false,"values":{}}'))
            self.assertEqual(db.execute("SELECT status FROM runtime_runs").fetchone()[0], "completed")
            self.assertEqual(db.execute("SELECT value FROM user_preferences WHERE key='theme'").fetchone()[0],
                             "synthetic-theme")
            self.assertEqual(db.execute("SELECT state FROM auth_state").fetchone()[0], "bootstrap_completed")
            self.assertEqual(db.execute("SELECT event_type,actor_user_id,correlation_id FROM auth_security_events "
                                        "WHERE event_type='historical_ownership_assigned'").fetchone(),
                             ("historical_ownership_assigned", "admin-fixture", "op-fixture-1"))
            self.assertEqual(db.execute("SELECT status FROM auth_historical_ownership_operations").fetchone()[0],
                             "committed")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_identical_retry_is_read_only_and_changed_payload_is_rejected(self):
        assign_historical_ownership(self.database, self.backup, self.consent)
        after = inspect_database(self.database).data_digest
        result = assign_historical_ownership(self.database, self.backup, self.consent)
        self.assertTrue(result.already_committed)
        self.assertEqual(inspect_database(self.database).data_digest, after)
        changed = consent(self.database, self.backup, "op-fixture-1", target="someone-else")
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup, changed)
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup, consent(
                self.database, self.backup, "different-operation"))

    def test_retry_blocks_unknown_postcommit_operation_states_without_writing(self):
        assign_historical_ownership(self.database, self.backup, self.consent)
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE runtime_interactions SET status='unknown' WHERE id='interaction-1'")
        before = inspect_database(self.database).data_digest
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup, self.consent)
        self.assertEqual(inspect_database(self.database).data_digest, before)

    def test_consent_manifest_and_backup_are_verified_before_any_write(self):
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup,
                                        consent(self.database, self.backup, confirmation=""))
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup, consent(
                self.database, self.backup, manifest_override={"version": 1}))
        bad_backup = self.root / "bad-backup.sqlite3"
        shutil.copyfile(self.backup, bad_backup)
        with sqlite3.connect(bad_backup) as db:
            db.execute("UPDATE conversations SET title='tampered backup'")
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, bad_backup, self.consent)
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.database, self.consent)
        self.assertIsNone(sqlite3.connect(self.database).execute(
            "SELECT owner_user_id FROM conversations").fetchone()[0])

    def test_missing_identity_and_existing_owner_fail_closed(self):
        missing = consent(self.database, self.backup, target="missing-user")
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup, missing)
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO auth_users VALUES('other','Other','other','hash','active',1,1)")
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup, self.consent)
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE auth_users SET status='disabled' WHERE id='other'")
            db.execute("UPDATE conversations SET owner_user_id='other'")
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup, self.consent)

    def test_stale_consented_inventory_and_preowned_target_rows_are_not_reassigned(self):
        stale = self.consent
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO conversations(id,title,created_at,updated_at) "
                       "VALUES('new-after-consent','synthetic','3','4')")
        before = inspect_database(self.database).data_digest
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup, stale)
        self.assertEqual(inspect_database(self.database).data_digest, before)

        case_root = self.root / "preowned-target"
        case_root.mkdir()
        database, backup = create_fixture(case_root)
        with sqlite3.connect(database) as db:
            db.execute("UPDATE conversations SET owner_user_id='admin-fixture'")
        before = inspect_database(database).data_digest
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(database, backup, consent(database, backup))
        self.assertEqual(inspect_database(database).data_digest, before)
    def test_schema_ledger_and_ambiguous_bootstrap_identity_fail_closed(self):
        cases = ("DROP INDEX idx_conversations_owner_updated",
                 "INSERT INTO schema_migrations VALUES(32,'future')",
                 "INSERT INTO auth_security_events(event_type,actor_user_id,created_at) "
                 "VALUES('bootstrap_completed','admin-fixture',2)")
        for index, statement in enumerate(cases):
            with self.subTest(statement=statement):
                case_root = self.root / f"schema-{index}"
                case_root.mkdir()
                database, backup = create_fixture(case_root)
                with sqlite3.connect(database) as db:
                    db.execute(statement)
                with self.assertRaises(AssignmentRejected):
                    assign_historical_ownership(database, backup, consent(database, backup))

    def test_completed_approvals_and_interactions_are_preserved(self):
        case_root = self.root / "terminal-ops"
        case_root.mkdir()
        database, backup = create_fixture(case_root,
                                          approval_status=("pending", "approved", "executed", "rejected"),
                                          interaction_status=("pending", "approved", "denied", "expired"))
        item = consent(database, backup)
        result = assign_historical_ownership(database, backup, item)
        self.assertEqual((result.approvals_invalidated, result.interactions_denied), (2, 1))
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT status FROM pending_approvals ORDER BY id").fetchall(),
                             [("invalidated",), ("invalidated",), ("executed",), ("rejected",)])
            self.assertEqual(db.execute("SELECT status FROM runtime_interactions ORDER BY id").fetchall(),
                             [("denied",), ("approved",), ("denied",), ("expired",)])

    def test_active_unknown_or_executing_operations_block_without_mutation(self):
        for run_status, approval_status, interaction_status in (
            ("running", "pending", "pending"),
            ("completed", "executing", "pending"),
            ("completed", "pending", "mystery"),
            ("waiting_approval", "approved", "pending"),
            ("mystery", None, None),
        ):
            with self.subTest(run=run_status, approval=approval_status, interaction=interaction_status):
                case_root = self.root / f"blocked-{len(list(self.root.iterdir()))}"
                case_root.mkdir()
                database, backup = create_fixture(case_root, run_status=run_status,
                                                  approval_status=approval_status,
                                                  interaction_status=interaction_status)
                before = inspect_database(database).data_digest
                with self.assertRaises(AssignmentRejected):
                    assign_historical_ownership(database, backup, consent(database, backup))
                self.assertEqual(inspect_database(database).data_digest, before)

    def test_relationship_orphans_and_invalid_fixture_paths_block(self):
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE conversations SET notebook_id='absent-notebook'")
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(self.database, self.backup, self.consent)
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership("/var/tmp/not-a-fixture.sqlite3", self.backup, self.consent)
        product_named = self.root / "nexo.sqlite3"
        shutil.copyfile(self.backup, product_named)
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(product_named, self.backup, self.consent)
        symlink = self.root / "linked-fixture.sqlite3"
        symlink.symlink_to(self.database)
        with self.assertRaises(AssignmentRejected):
            assign_historical_ownership(symlink, self.backup, self.consent)
        data_dir = self.root / "configured-data"
        data_dir.mkdir()
        blocked, blocked_backup = create_fixture(data_dir)
        with patch.dict(os.environ, {"NEXO_DATA_DIR": str(data_dir)}):
            with self.assertRaises(AssignmentRejected):
                assign_historical_ownership(blocked, blocked_backup, consent(blocked, blocked_backup))
        self.assertEqual(inspect_database(self.database).version, 31)

    def test_faults_at_each_write_phase_roll_back_all_domain_and_audit_changes(self):
        for stage in ("after_ownership", "after_invalidation", "after_audit", "before_commit"):
            with self.subTest(stage=stage):
                case_root = self.root / f"fault-{stage}"
                case_root.mkdir()
                database, backup = create_fixture(case_root)
                before = inspect_database(database).data_digest

                def fail(current):
                    if current == stage:
                        raise RuntimeError("injected synthetic failure")

                with self.assertRaisesRegex(RuntimeError, "injected synthetic"):
                    assign_historical_ownership(database, backup, consent(database, backup), fault_hook=fail)
                self.assertEqual(inspect_database(database).data_digest, before)
                with sqlite3.connect(database) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_security_events "
                                                "WHERE event_type='historical_ownership_assigned'").fetchone()[0], 0)
                    self.assertIsNone(db.execute("SELECT owner_user_id FROM conversations").fetchone()[0])
                self.assertFalse(assign_historical_ownership(database, backup,
                                                             consent(database, backup)).already_committed)

    def test_sql_failures_during_second_owner_write_and_audit_roll_back_everything(self):
        for patch_target, fail_on in (
            ("prototypes.historical_ownership_assignment._assign", "notebooks"),
            ("prototypes.historical_ownership_assignment.record_historical_ownership_audit", "audit"),
        ):
            with self.subTest(patch_target=patch_target):
                case_root = self.root / f"sql-fault-{len(list(self.root.iterdir()))}"
                case_root.mkdir()
                database, backup = create_fixture(case_root)
                before = inspect_database(database).data_digest
                if fail_on == "notebooks":
                    from prototypes import historical_ownership_assignment as assignment
                    original = assignment._assign

                    def inject(connection, table, ids, target):
                        if table == "notebooks":
                            raise sqlite3.OperationalError("synthetic ownership SQL failure")
                        return original(connection, table, ids, target)

                    with patch(patch_target, side_effect=inject), self.assertRaises(sqlite3.OperationalError):
                        assign_historical_ownership(database, backup, consent(database, backup))
                else:
                    with patch(patch_target, side_effect=sqlite3.IntegrityError("synthetic audit insert failure")), \
                            self.assertRaises(sqlite3.IntegrityError):
                        assign_historical_ownership(database, backup, consent(database, backup))
                self.assertEqual(inspect_database(database).data_digest, before)

    def test_cooperating_runtime_shared_lease_blocks_the_prototype(self):
        with acquire_runtime_lease(self.database):
            with self.assertRaises(DatabaseInUse):
                assign_historical_ownership(self.database, self.backup, self.consent)
        with sqlite3.connect(self.database) as db:
            self.assertIsNone(db.execute("SELECT owner_user_id FROM conversations").fetchone()[0])

    def test_concurrent_same_and_different_operation_ids_cannot_double_claim(self):
        gate = Barrier(2)

        def run(operation_id):
            item = consent(self.database, self.backup, operation_id)
            gate.wait()
            try:
                return assign_historical_ownership(self.database, self.backup, item).already_committed
            except AssignmentRejected:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(lambda _i: run("same-op"), range(2))), [False, True])
        self.assertEqual(sqlite3.connect(self.database).execute(
            "SELECT COUNT(*) FROM auth_security_events WHERE event_type='historical_ownership_assigned'").fetchone()[0], 1)

        root = self.root / "different-ids"
        root.mkdir()
        database, backup = create_fixture(root)
        gate = Barrier(2)

        def race(operation_id):
            item = consent(database, backup, operation_id)
            gate.wait()
            try:
                return assign_historical_ownership(database, backup, item).already_committed
            except AssignmentRejected:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(race, ("operation-a", "operation-b")))
        self.assertCountEqual(outcomes, [False, "rejected"])
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_security_events "
                                        "WHERE event_type='historical_ownership_assigned'").fetchone()[0], 1)

    def test_sigkill_before_commit_rolls_back_and_after_commit_retries_idempotently(self):
        for stage, committed in (("before_commit", False), ("after_commit", True)):
            with self.subTest(stage=stage):
                case_root = self.root / f"kill-{stage}"
                case_root.mkdir()
                database, backup = create_fixture(case_root)
                item = consent(database, backup)
                payload = json.dumps({**item.__dict__, "conversation_ids": list(item.conversation_ids),
                                      "notebook_ids": list(item.notebook_ids), "manifest": item.manifest})
                script = """import json,sys
from prototypes.historical_ownership_assignment import SyntheticAssignmentConsent,assign_historical_ownership
p=json.loads(sys.argv[3]); p['conversation_ids']=tuple(p['conversation_ids']); p['notebook_ids']=tuple(p['notebook_ids'])
def stop(stage):
 if stage==sys.argv[4]:
  print('READY',flush=True)
  while True: pass
assign_historical_ownership(sys.argv[1],sys.argv[2],SyntheticAssignmentConsent(**p),fault_hook=stop)
"""
                process = subprocess.Popen([sys.executable, "-c", script, str(database), str(backup),
                                            payload, stage], cwd=Path(__file__).parents[1],
                                           stdout=subprocess.PIPE, text=True)
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(30))
                self.assertEqual(process.stdout.readline().strip(), "READY")
                process.send_signal(signal.SIGKILL)
                self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
                selector.close()
                with sqlite3.connect(database) as db:
                    count = 1 if committed else 0
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0], count)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_security_events "
                                                "WHERE event_type='historical_ownership_assigned'").fetchone()[0], count)
                    self.assertEqual(db.execute("SELECT owner_user_id FROM conversations").fetchone()[0],
                                     "admin-fixture" if committed else None)
                retry = assign_historical_ownership(database, backup, item)
                self.assertEqual(retry.already_committed, committed)
                state = inspect_database(database)
                self.assertEqual(state.version, 31)
                with sqlite3.connect(database) as db:
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
