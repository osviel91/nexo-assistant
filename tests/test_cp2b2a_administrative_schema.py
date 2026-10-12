import os
import selectors
import signal
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.safe_migrations import SafeMigrationEngine, SchemaCompatibilityError, inspect_database
from prototypes.admin_persistence import backup_database, restore_database
from tests.test_cp2b1_safe_migrations import create_historical_database


class AdministrativeSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_and_v28_bases_upgrade_to_empty_bootstrap_pending_v30(self):
        for base in ("fresh", "v28"):
            with self.subTest(base=base):
                data_dir = self.root / base
                data_dir.mkdir()
                database = data_dir / "nexo.sqlite3"
                if base == "v28":
                    create_historical_database(database, 28)
                result = SafeMigrationEngine(data_dir).migrate()
                self.assertEqual((result.source_version, result.target_version), (0 if base == "fresh" else 28, 31))
                self.assertEqual(inspect_database(database).version, 31)
                with sqlite3.connect(database) as connection:
                    self.assertEqual(connection.execute("SELECT state FROM auth_state").fetchone()[0], "schema_installed")
                    for table in ("auth_users", "auth_user_permissions", "auth_sessions", "auth_security_events"):
                        self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                    connection.execute("PRAGMA foreign_keys=ON")
                    self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_constraints_and_foreign_keys_reject_invalid_admin_rows(self):
        data_dir = self.root / "constraints"
        data_dir.mkdir()
        SafeMigrationEngine(data_dir).migrate()
        with sqlite3.connect(data_dir / "nexo.sqlite3") as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for sql, values in (
                ("INSERT INTO auth_state VALUES(2,'bootstrap_pending',1,NULL)", ()),
                ("INSERT INTO auth_users VALUES('u','u','u','hash','superuser','now','now')", ()),
                ("INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,?)", (b'bad', b'x' * 32, 'missing', '1', '1', '2', '3', None)),
                ("INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,?)", (b't' * 32, b'c' * 32, 'missing', 1, 1, 2, 3, None)),
                ("INSERT INTO auth_user_permissions VALUES('missing','chat:read')", ()),
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql, values)
            connection.execute("INSERT INTO auth_users VALUES('u','user','user','scrypt$v1$fixture','active',1,1)")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_users VALUES('u','other','other','hash','active',1,1)")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_users VALUES('u2','other','user','hash','active',1,1)")
            connection.execute("INSERT INTO auth_user_permissions VALUES('u','chat:read')")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_user_permissions VALUES('u','chat:read')")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_user_permissions VALUES('u','*')")
            session = (b't' * 32, b'c' * 32, 'u', 1, 1, 2, 3, None)
            connection.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,?)", session)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,?)", session)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,?)",
                                   (b'short', b'c' * 32, 'u', 1, 1, 2, 3, None))

    def test_completed_bootstrap_session_revocation_and_rate_limit_state_validate(self):
        data_dir = self.root / "valid-state"
        data_dir.mkdir()
        SafeMigrationEngine(data_dir).migrate()
        database = data_dir / "nexo.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("INSERT INTO auth_users VALUES('u','user','user','scrypt$v1$fixture','active',1,1)")
            connection.execute("INSERT INTO auth_user_permissions VALUES('u','chat:read')")
            connection.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) "
                               "VALUES('bootstrap_completed','u',2)")
            connection.execute("UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at='2' WHERE id=1")
            connection.execute("UPDATE auth_login_guard SET failures=3,window_started_at=10,locked_until=20 "
                               "WHERE subject='administrator'")
            connection.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,?)",
                               (b't' * 32, b'c' * 32, 'u', 1, 1, 2, 3, 2))
        self.assertEqual(inspect_database(database).version, 31)
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("SELECT failures,window_started_at,locked_until "
                                                 "FROM auth_login_guard").fetchone(), (3, 10.0, 20.0))
            self.assertEqual(connection.execute("SELECT revoked_at FROM auth_sessions").fetchone()[0], 2.0)

    def test_unknown_and_inconsistent_bootstrap_states_fail_runtime_validation(self):
        mutations = (
            "UPDATE auth_state SET state='mystery' WHERE id=1",
            "UPDATE auth_state SET state='schema_installed',bootstrapped_at=1 WHERE id=1",
            "UPDATE auth_state SET state='bootstrap_pending',bootstrapped_at=1 WHERE id=1",
            "UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at=NULL WHERE id=1",
            "UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at=1 WHERE id=1",
            "INSERT INTO auth_users VALUES('u','user','user','scrypt$v1$fixture','active',1,1)",
            "UPDATE auth_login_guard SET failures=-1 WHERE subject='administrator'",
            "INSERT INTO auth_login_guard VALUES('unrecognized',0,0,0)",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                data_dir = self.root / str(len(list(self.root.iterdir())))
                data_dir.mkdir()
                SafeMigrationEngine(data_dir).migrate()
                database = data_dir / "nexo.sqlite3"
                with sqlite3.connect(database) as connection:
                    connection.execute("PRAGMA ignore_check_constraints=ON")
                    connection.execute(mutation)
                with self.assertRaises(SchemaCompatibilityError):
                    inspect_database(database)

    def test_incomplete_v30_and_extra_objects_fail_schema_validation(self):
        for change in ("DROP TABLE auth_sessions", "DROP INDEX idx_auth_sessions_user",
                       "CREATE TABLE unexpected_admin_object(id INTEGER)"):
            with self.subTest(change=change):
                data_dir = self.root / str(len(list(self.root.iterdir())))
                data_dir.mkdir()
                SafeMigrationEngine(data_dir).migrate()
                database = data_dir / "nexo.sqlite3"
                with sqlite3.connect(database) as connection:
                    connection.execute(change)
                with self.assertRaises(SchemaCompatibilityError):
                    inspect_database(database)

    def test_v30_backup_restore_and_security_event_append_only(self):
        data_dir = self.root / "backup"
        data_dir.mkdir()
        database = data_dir / "nexo.sqlite3"
        SafeMigrationEngine(data_dir).migrate()
        backup = self.root / "backup.sqlite3"
        restored = self.root / "restored.sqlite3"
        synthetic_source = self.root / "nexo-fixture.sqlite3"
        shutil.copyfile(database, synthetic_source)
        backup_database(synthetic_source, backup)
        restore_database(backup, restored)
        self.assertEqual(inspect_database(restored).version, 31)
        with sqlite3.connect(restored) as connection:
            connection.execute("INSERT INTO auth_security_events(event_type,created_at) VALUES('login_failed',1)")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE auth_security_events SET event_type='logout'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM auth_security_events")

    def test_fault_before_ledger_leaves_canonical_v28_unchanged_then_retry_succeeds(self):
        data_dir = self.root / "fault"
        data_dir.mkdir()
        database = data_dir / "nexo.sqlite3"
        create_historical_database(database, 28)
        before = database.read_bytes()

        def fail(stage):
            if stage == "before_ledger":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            SafeMigrationEngine(data_dir).migrate(fault_hook=fail)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(inspect_database(database).version, 28)
        self.assertEqual(inspect_database(SafeMigrationEngine(data_dir).migrate().backup_path).version, 28)

    def test_sigkill_before_and_after_transaction_commit_preserve_canonical_then_retry(self):
        for barrier in ("after_ledger", "after_commit"):
            with self.subTest(barrier=barrier):
                data_dir = self.root / barrier
                data_dir.mkdir()
                database = data_dir / "nexo.sqlite3"
                create_historical_database(database, 28)
                code = """import sys
from app.safe_migrations import SafeMigrationEngine
def stop(stage):
    if stage == sys.argv[2]:
        print('READY', flush=True)
        while True: pass
SafeMigrationEngine(sys.argv[1]).migrate(fault_hook=stop)
"""
                child = subprocess.Popen([sys.executable, "-c", code, str(data_dir), barrier],
                                         stdout=subprocess.PIPE, text=True)
                selector = selectors.DefaultSelector()
                selector.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(20), "child did not reach transaction barrier")
                self.assertEqual(child.stdout.readline().strip(), "READY")
                child.send_signal(signal.SIGKILL)
                self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)
                selector.close()
                self.assertEqual(inspect_database(database).version, 28)
                stage = next(data_dir.glob(".nexo.sqlite3.stage.*.sqlite3"))
                staged = inspect_database(stage)
                expected_stage_version = 28 if barrier == "after_ledger" else 29
                self.assertEqual(staged.version, expected_stage_version)
                with sqlite3.connect(stage) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(tuple(row[0] for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version")),
                        tuple(range(1, expected_stage_version + 1)))
                    self.assertEqual(connection.execute("SELECT name FROM providers "
                                                        "WHERE id='fixture-provider'").fetchone()[0], "synthetic")
                    if barrier == "after_ledger":
                        self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master "
                                                             "WHERE name='auth_state'").fetchone())
                    else:
                        self.assertEqual(connection.execute("SELECT state FROM auth_state").fetchone()[0],
                                         "schema_installed")
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 0)
                result = SafeMigrationEngine(data_dir).migrate()
                self.assertEqual(result.target_version, 31)
                self.assertEqual(inspect_database(database).version, 31)


if __name__ == "__main__":
    unittest.main()
