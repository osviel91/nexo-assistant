import sqlite3
from dataclasses import replace
import selectors
import signal
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from pathlib import Path

from app.historical_ownership_audit import HistoricalOwnershipAudit, record_historical_ownership_audit
from app.safe_migrations import (DatabaseLease, SafeMigrationEngine, SchemaCompatibilityError,
                                TRANSACTIONAL_MIGRATIONS, _apply_transactional_migration,
                                expected_historical_audit_schema_manifest, inspect_database,
                                validate_schema_manifest)
from tests.test_cp2b1_safe_migrations import create_historical_database


class HistoricalOwnershipAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        SafeMigrationEngine(self.directory).migrate()
        self.database = self.directory / "nexo.sqlite3"
        self.audit = HistoricalOwnershipAudit("op-1", "admin-1", 10.0, 4, 2,
                                              "a" * 64, "b" * 64, "backup-1", "c" * 64,
                                              "generation-1")
        with sqlite3.connect(self.database) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("INSERT INTO auth_users VALUES('admin-1','Admin','admin','hash','active',1,1)")
            db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES('c1','x','1','2')")
            db.execute("INSERT INTO notebooks(id,name,created_at,updated_at) VALUES('n1','x','1','2')")

    def tearDown(self):
        self.temp.cleanup()

    def test_audit_event_and_operation_commit_or_rollback_together_and_retry_is_exact(self):
        with sqlite3.connect(self.database) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            self.assertTrue(record_historical_ownership_audit(db, self.audit))
            self.assertTrue(db.in_transaction)
            db.execute("UPDATE conversations SET title='synthetic assigned' WHERE id='c1'")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            self.assertFalse(record_historical_ownership_audit(db, self.audit))
            with self.assertRaises(ValueError):
                record_historical_ownership_audit(db, HistoricalOwnershipAudit(
                    "op-1", "admin-1", 10.0, 5, 2, "a" * 64, "b" * 64,
                    "backup-1", "c" * 64, "generation-1"))
            db.rollback()
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.IntegrityError):
                record_historical_ownership_audit(db, HistoricalOwnershipAudit(
                    "op-3", "missing", 11.0, 4, 2, "a" * 64, "b" * 64,
                    "backup-1", "c" * 64, "generation-1"))
            db.rollback()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_security_events WHERE event_type='historical_ownership_assigned'").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT owner_user_id FROM conversations WHERE id='c1'").fetchone()[0])
            self.assertIsNone(db.execute("SELECT owner_user_id FROM notebooks WHERE id='n1'").fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE auth_historical_ownership_operations SET status='failed'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM auth_historical_ownership_operations")

    def test_failed_commit_rolls_back_operation_and_generated_event(self):
        with sqlite3.connect(self.database, isolation_level=None) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TEMP TABLE commit_parent(id INTEGER PRIMARY KEY)")
            db.execute("CREATE TEMP TABLE commit_child(parent_id INTEGER REFERENCES commit_parent(id) "
                       "DEFERRABLE INITIALLY DEFERRED)")
            db.execute("BEGIN IMMEDIATE")
            record_historical_ownership_audit(db, self.audit)
            db.execute("INSERT INTO commit_child VALUES(999)")
            with self.assertRaises(sqlite3.IntegrityError):
                db.commit()
            db.rollback()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_security_events "
                                        "WHERE event_type='historical_ownership_assigned'").fetchone()[0], 0)

    def test_migration_preserves_history_triggers_index_and_append_only(self):
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO auth_user_permissions VALUES('admin-1','chat:read')")
            db.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) "
                       "VALUES('bootstrap_completed','admin-1',1)")
            db.execute("UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at='1' WHERE id=1")
        result = SafeMigrationEngine(self.directory).migrate()
        self.assertEqual(result.target_version, 31)
        self.assertEqual(inspect_database(self.database).version, 31)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT event_type FROM auth_security_events").fetchall(), [("bootstrap_completed",)])
            self.assertEqual({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='auth_security_events'" )},
                             {"auth_security_events_no_update", "auth_security_events_no_delete",
                              "auth_security_events_require_historical_operation"})
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_auth_security_events_created'").fetchone())
            for statement in ("UPDATE auth_security_events SET event_type='logout'", "DELETE FROM auth_security_events"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO auth_security_events(event_type,created_at) VALUES('unknown',1)")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at,correlation_id) "
                           "VALUES('historical_ownership_assigned','admin-1',1,'incomplete')")
            db.execute("INSERT INTO auth_historical_ownership_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("direct-op", "admin-1", 2, 0, 0, "a" * 64, "b" * 64,
                        "backup", "c" * 64, "generation", 1, "committed"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_security_events "
                                        "WHERE correlation_id='direct-op'").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at,correlation_id) "
                           "SELECT event_type,actor_user_id,created_at,correlation_id FROM auth_security_events "
                           "WHERE event_type='historical_ownership_assigned'")

    def test_v31_manifest_detects_check_unique_foreign_key_index_and_trigger_drift(self):
        expected = expected_historical_audit_schema_manifest()
        for name, changed in (
            ("check", replace(expected, objects=tuple(
                replace(item, sql=(item.sql or "") + " altered")
                if item.kind == "table" and item.name == "auth_historical_ownership_operations" else item
                for item in expected.objects))),
            ("unique-index", replace(expected,
                objects=tuple(item for item in expected.objects
                              if item.name != "idx_auth_security_events_historical_operation"))),
            ("foreign-key", replace(expected, tables=tuple(
                replace(table, foreign_keys=()) if table.name == "auth_historical_ownership_operations" else table
                for table in expected.tables))),
            ("index", replace(expected,
                objects=tuple(item for item in expected.objects
                              if item.name != "idx_auth_security_events_created"))),
            ("trigger", replace(expected,
                objects=tuple(item for item in expected.objects
                              if item.name != "auth_historical_ownership_operations_create_event"))),
        ):
            with self.subTest(name=name), sqlite3.connect(self.database) as db:
                with self.assertRaises(SchemaCompatibilityError):
                    validate_schema_manifest(db, changed)

    def test_v30_to_v31_sigkill_after_promotion_recovers_through_safe_engine(self):
        migration_dir = self.directory / "v30-promotion"
        migration_dir.mkdir()
        database = migration_dir / "nexo.sqlite3"
        create_historical_database(database, 28)
        connection = sqlite3.connect(database, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with DatabaseLease(database, exclusive=True) as lease:
                for spec in TRANSACTIONAL_MIGRATIONS[:2]:
                    _apply_transactional_migration(connection, lease, spec.version, spec.statements,
                                                   spec.validate_previous, spec.validate_output)
        finally:
            connection.close()
        with sqlite3.connect(database) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("INSERT INTO auth_users VALUES('admin-1','Admin','admin','hash','active',1,1)")
            db.execute("INSERT INTO auth_user_permissions VALUES('admin-1','chat:read')")
            db.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) "
                       "VALUES('bootstrap_completed','admin-1',1)")
            db.execute("UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at='1' WHERE id=1")
        self.assertEqual(inspect_database(database).version, 30)

        code = """import sys
from app.safe_migrations import SafeMigrationEngine
def stop(stage):
 if stage == 'after_replace':
  print('READY',flush=True)
  while True: pass
SafeMigrationEngine(sys.argv[1]).migrate(fault_hook=stop)
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(migration_dir)], stdout=subprocess.PIPE, text=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        self.assertTrue(selector.select(20))
        self.assertEqual(process.stdout.readline().strip(), "READY")
        process.send_signal(signal.SIGKILL)
        self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
        selector.close()

        result = SafeMigrationEngine(migration_dir).migrate()
        self.assertEqual((result.source_version, result.target_version, result.changed), (31, 31, False))
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT event_type,actor_user_id FROM auth_security_events").fetchall(),
                             [("bootstrap_completed", "admin-1")])
            self.assertEqual(tuple(row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")),
                             tuple(range(1, 32)))

    def test_missing_fields_rejected_and_audit_insert_failure_rolls_back(self):
        with sqlite3.connect(self.database) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                record_historical_ownership_audit(db, HistoricalOwnershipAudit(
                    "op", "admin-1", 1, 0, 0, "bad", "b" * 64, "backup", "c" * 64, "gen"))
            db.rollback()
            db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON auth_security_events "
                       "WHEN NEW.event_type='historical_ownership_assigned' BEGIN SELECT RAISE(ABORT,'injected'); END")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.IntegrityError):
                record_historical_ownership_audit(db, self.audit)
            db.rollback()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0], 0)

    def test_sigkill_before_and_after_synthetic_operation_commit(self):
        for barrier, committed in (("before_commit", False), ("after_commit", True)):
            with self.subTest(barrier=barrier):
                code = """import sqlite3,sys
from app.historical_ownership_audit import HistoricalOwnershipAudit,record_historical_ownership_audit
db=sqlite3.connect(sys.argv[1],isolation_level=None); db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN IMMEDIATE')
db.execute(\"UPDATE conversations SET title='synthetic' WHERE id='c1'\")
a=HistoricalOwnershipAudit('crash-op','admin-1',20.0,1,0,'a'*64,'b'*64,'backup','c'*64,'gen')
record_historical_ownership_audit(db,a)
if sys.argv[2]=='before_commit':
 print('READY',flush=True)
 while True: pass
db.commit(); print('READY',flush=True)
while True: pass
"""
                process = subprocess.Popen([sys.executable, "-c", code, str(self.database), barrier],
                                           stdout=subprocess.PIPE, text=True)
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(20))
                self.assertEqual(process.stdout.readline().strip(), "READY")
                process.send_signal(signal.SIGKILL)
                self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
                selector.close()
                with sqlite3.connect(self.database) as db:
                    expected_count = 1 if committed else 0
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations WHERE operation_id='crash-op'").fetchone()[0], expected_count)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_security_events WHERE correlation_id='crash-op'").fetchone()[0], expected_count)
                    self.assertIsNone(db.execute("SELECT owner_user_id FROM conversations WHERE id='c1'").fetchone()[0])

    def test_concurrent_identical_retries_create_one_operation_and_event(self):
        barrier = Barrier(2)

        def write():
            with sqlite3.connect(self.database, timeout=5, isolation_level=None) as db:
                db.execute("PRAGMA foreign_keys=ON")
                barrier.wait()
                db.execute("BEGIN IMMEDIATE")
                inserted = record_historical_ownership_audit(db, self.audit)
                db.commit()
                return inserted

        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sorted(workers.map(lambda _index: write(), range(2))), [False, True])
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_security_events WHERE event_type='historical_ownership_assigned'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
