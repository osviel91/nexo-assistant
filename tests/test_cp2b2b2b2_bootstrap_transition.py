import hashlib
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from unittest.mock import patch

from app.safe_migrations import SafeMigrationEngine, SafeMigrationError, inspect_database
import prototypes.bootstrap_transition_simulator as transition_simulator
from prototypes.bootstrap_transition_simulator import (
    ExecutionFence,
    FIXTURE_CONFIRMATION,
    REQUIRED_ADMIN_PERMISSIONS,
    TransitionConsent,
    TransitionRejected,
    _relationship_snapshot,
    canonical_sha256,
    commit_synthetic_transition,
)
from prototypes.ownership_preparation import SYNTHETIC_APPLICATION_ID


def synthetic_pair(root: Path, *, run_status: str = "completed", completed_at: str | None = "2"):
    directory = root / "fixture"
    directory.mkdir()
    SafeMigrationEngine(directory).migrate()
    database = directory / "nexo.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA application_id={SYNTHETIC_APPLICATION_ID}")
        connection.execute("INSERT INTO notebooks(id,name,description,created_at,updated_at) "
                           "VALUES('n1','Synthetic','fixture','1','1')")
        connection.execute("INSERT INTO conversations(id,title,created_at,updated_at) "
                           "VALUES('c1','Synthetic','1','1')")
        if run_status != "none":
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) "
                               "VALUES('m1','c1','user','fixture','1')")
            connection.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,completed_at,status) "
                               "VALUES('r1','c1','m1','1',?,?)",
                               (completed_at if run_status in {"completed", "failed"} else None, run_status))
            connection.execute("INSERT INTO pending_approvals "
                               "(id,run_id,conversation_id,message_id,tool_call_id,tool_id,action,tool_fingerprint,"
                               "frozen_arguments,arguments_sha256,safe_summary,status,created_at) "
                               "VALUES('a1','r1','c1','m1','call1','tool1','mutating','fingerprint','{}',"
                               "'sha','{}','pending','1')")
            connection.execute("INSERT INTO runtime_interactions "
                               "(id,run_id,conversation_id,kind,payload,status,created_at,expires_at) "
                               "VALUES('i1','r1','c1','form','{}','pending','1','9999')")
    fixture = directory / "synthetic-database.sqlite3"
    database.rename(fixture)
    backup = directory / "backup.sqlite3"
    shutil.copyfile(fixture, backup)
    before = inspect_database(fixture)
    with backup.open("rb") as backup_file:
        backup_sha = hashlib.file_digest(backup_file, "sha256").hexdigest()
    manifest = {
        "version": 1, "operation_id": "transition-1", "database_generation": before.data_digest,
        "target_user_id": "synthetic-admin", "backup_id": backup.name, "backup_sha256": backup_sha,
        "conversation_ids": ["c1"], "notebook_ids": ["n1"],
        "conversation_count": 1, "notebook_count": 1,
        "approval_ids_to_invalidate": ["a1"], "interaction_ids_to_deny": ["i1"],
    }
    with sqlite3.connect(fixture) as connection:
        manifest["relationships"] = _relationship_snapshot(connection)
    consent = TransitionConsent(
        FIXTURE_CONFIRMATION, "transition-1", before.data_digest, "synthetic-admin", "fixture-admin",
        "synthetic-password-hash", REQUIRED_ADMIN_PERMISSIONS, backup.name, backup_sha,
        manifest, canonical_sha256(manifest), 1234.0,
    )
    fence = ExecutionFence(True, True, True, True, True, 2000.0, 1234.0)
    return fixture, backup, consent, fence


class BootstrapTransitionSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database, self.backup, self.consent, self.fence = synthetic_pair(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_first_admin_authority_ownership_audit_and_bootstrap_commit_together(self):
        self.assertTrue(commit_synthetic_transition(self.database, self.backup, self.consent, self.fence))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT state FROM auth_state").fetchone()[0], "bootstrap_completed")
            self.assertEqual(connection.execute("SELECT owner_user_id FROM conversations").fetchone()[0], "synthetic-admin")
            self.assertEqual(connection.execute("SELECT owner_user_id FROM notebooks").fetchone()[0], "synthetic-admin")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_user_permissions").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_security_events WHERE event_type='historical_ownership_assigned'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_security_events WHERE event_type='bootstrap_completed'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT status FROM pending_approvals").fetchone()[0], "invalidated")
            self.assertEqual(connection.execute("SELECT status FROM runtime_interactions").fetchone()[0], "denied")
        inspect_database(self.database)

    def test_retry_is_idempotent_but_contradictory_consent_is_rejected(self):
        commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        self.assertFalse(commit_synthetic_transition(self.database, self.backup, self.consent, self.fence))
        changed = TransitionConsent(**(self.consent.__dict__ | {"username": "different"}))
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, changed, self.fence)

    def test_incomplete_authority_or_execution_fence_fails_before_mutation(self):
        weak = TransitionConsent(**(self.consent.__dict__ | {"grants": frozenset({"chat:read"})}))
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, weak, self.fence)
        unallowlisted = TransitionConsent(**(self.consent.__dict__ | {
            "grants": REQUIRED_ADMIN_PERMISSIONS | {"system:admin"},
        }))
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, unallowlisted, self.fence)
        invalid_fence = ExecutionFence(True, True, True, False, True, 2000.0, 1234.0)
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, self.consent, invalid_fence)
        expired = ExecutionFence(True, True, True, True, True, 1234.0, 1234.0)
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, self.consent, expired)
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, None, self.fence)
        self.assertEqual(inspect_database(self.database).data_digest, self.consent.database_generation)

    def test_active_executor_and_unmarked_database_are_rejected(self):
        active_dir = self.root / "active"
        active_dir.mkdir()
        active_db, active_backup, active_consent, fence = synthetic_pair(active_dir, run_status="running")
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(active_db, active_backup, active_consent, fence)
        unmarked = self.root / "unmarked.sqlite3"
        shutil.copyfile(self.database, unmarked)
        with sqlite3.connect(unmarked) as connection:
            connection.execute("PRAGMA application_id=0")
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(unmarked, self.backup, self.consent, self.fence)

    def test_terminal_run_without_terminal_timestamp_is_rejected(self):
        malformed_dir = self.root / "malformed-run"
        malformed_dir.mkdir()
        database, backup, consent, fence = synthetic_pair(
            malformed_dir, run_status="completed", completed_at=None)
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(database, backup, consent, fence)
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("SELECT state FROM auth_state").fetchone()[0], "schema_installed")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 0)

    def test_stale_backup_or_manifest_is_rejected(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE conversations SET title='changed'")
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        self.assertEqual(inspect_database(self.database).version, 31)

    def test_backup_tampering_and_different_manifest_are_rejected(self):
        changed_manifest = self.consent.manifest | {"notebook_count": 2}
        changed = TransitionConsent(**(self.consent.__dict__ | {"manifest": changed_manifest}))
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, changed, self.fence)
        with sqlite3.connect(self.backup) as connection:
            connection.execute("UPDATE conversations SET title='tampered backup'")
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)

    def test_concurrent_identical_attempts_have_at_most_one_writer(self):
        barrier = Barrier(2)

        def attempt():
            barrier.wait()
            try:
                return commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: attempt(), range(2)))
        self.assertEqual(sum(result is True for result in results), 1)
        self.assertTrue(all(isinstance(result, (bool, SafeMigrationError)) for result in results))
        self.assertFalse(commit_synthetic_transition(self.database, self.backup, self.consent, self.fence))

    def test_partial_bootstrap_state_does_not_validate_as_ready(self):
        commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE auth_state SET state='schema_installed',bootstrapped_at=NULL")
        with self.assertRaises(SafeMigrationError):
            inspect_database(self.database)

    def test_explicit_consent_is_required_and_not_inferred_from_manifest(self):
        no_consent = TransitionConsent(**(self.consent.__dict__ | {"confirmation": ""}))
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, no_consent, self.fence)

    def test_failure_before_commit_rolls_back_every_bootstrap_write(self):
        broken = TransitionConsent(**(self.consent.__dict__ | {
            "manifest": self.consent.manifest | {"conversation_count": 999},
        }))
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, broken, self.fence)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT state FROM auth_state").fetchone()[0], "schema_installed")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT owner_user_id FROM conversations").fetchone()[0])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_security_events").fetchone()[0], 0)

    def test_audit_sql_failure_rolls_back_identity_ownership_and_invalidations(self):
        with patch.object(transition_simulator, "record_historical_ownership_audit",
                          side_effect=sqlite3.IntegrityError("synthetic audit failure")):
            with self.assertRaises(sqlite3.IntegrityError):
                commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT state FROM auth_state").fetchone()[0], "schema_installed")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT owner_user_id FROM conversations").fetchone()[0])
            self.assertEqual(connection.execute("SELECT status FROM pending_approvals").fetchone()[0], "pending")
            self.assertEqual(connection.execute("SELECT status FROM runtime_interactions").fetchone()[0], "pending")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_security_events").fetchone()[0], 0)

    def test_writer_between_snapshot_and_begin_is_rejected(self):
        original_connect = sqlite3.connect
        mutated = False

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                self.connection.__enter__()
                return self

            def __exit__(self, *args):
                return self.connection.__exit__(*args)

            def execute(self, sql, *args):
                nonlocal mutated
                if sql == "BEGIN IMMEDIATE" and not mutated:
                    with original_connect(self.database) as competing_writer:
                        competing_writer.execute(
                            "UPDATE conversations SET title='concurrent synthetic change'")
                    mutated = True
                return self.connection.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        ConnectionProxy.database = self.database
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            database_argument = str(args[0])
            is_database_connection = (not database_argument.startswith("file:")
                                      and Path(args[0]).resolve() == self.database.resolve())
            return ConnectionProxy(connection) if is_database_connection else connection

        rejected = False
        with patch.object(transition_simulator, "sqlite3", SimpleNamespace(connect=connect)):
            try:
                commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
            except TransitionRejected:
                rejected = True
        self.assertTrue(mutated)
        self.assertTrue(rejected)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT owner_user_id FROM conversations").fetchone()[0])

    def test_locked_fingerprint_is_computed_within_the_write_transaction(self):
        inspect_locked = transition_simulator._inspect_connection
        observed_transaction = False

        def inspect_and_record(connection, *, fingerprint_data):
            nonlocal observed_transaction
            observed_transaction = connection.in_transaction and fingerprint_data
            return inspect_locked(connection, fingerprint_data=fingerprint_data)

        with patch.object(transition_simulator, "_inspect_connection", side_effect=inspect_and_record):
            self.assertTrue(commit_synthetic_transition(self.database, self.backup, self.consent, self.fence))
        self.assertTrue(observed_transaction)

    def test_identical_retry_rejects_ownership_moved_to_another_user(self):
        commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO auth_users VALUES(?,?,?,?,?,?,?)", (
                "other-user", "other", "other", "synthetic-hash", "active", 1, 1))
            connection.execute("INSERT INTO auth_user_permissions VALUES(?,?)", ("other-user", "chat:read"))
            connection.execute("UPDATE conversations SET owner_user_id='other-user'")
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)

    def test_identical_retry_rejects_missing_manifest_resource_without_writes(self):
        commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM notebooks WHERE id='n1'")
        before = inspect_database(self.database).data_digest
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        self.assertEqual(inspect_database(self.database).data_digest, before)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_security_events WHERE event_type='historical_ownership_assigned'").fetchone()[0], 1)

    def test_identical_retry_rejects_changed_relationship_without_writes(self):
        commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE conversations SET notebook_id='n1' WHERE id='c1'")
        before = inspect_database(self.database).data_digest
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        self.assertEqual(inspect_database(self.database).data_digest, before)

    def test_identical_retry_rejects_changed_manifest_count_without_writes(self):
        commit_synthetic_transition(self.database, self.backup, self.consent, self.fence)
        broken_manifest = self.consent.manifest | {"conversation_count": 2}
        broken = TransitionConsent(**(self.consent.__dict__ | {
            "manifest": broken_manifest,
            "manifest_sha256": canonical_sha256(broken_manifest),
        }))
        before = inspect_database(self.database).data_digest
        with self.assertRaises(TransitionRejected):
            commit_synthetic_transition(self.database, self.backup, broken, self.fence)
        self.assertEqual(inspect_database(self.database).data_digest, before)


if __name__ == "__main__":
    unittest.main()
