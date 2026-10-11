import concurrent.futures
from contextlib import contextmanager
import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.migrations import migrate
from prototypes.admin_auth import ADMIN_PERMISSIONS, AdminAuthStore, AuthFailure
from prototypes.admin_persistence import (
    AdministrativePersistence,
    BackupFailure,
    IncompatibleSchema,
    OwnershipRequired,
    PersistenceConflict,
    PersistenceUnavailable,
    backup_database,
    restore_database,
    verify_sqlite_file,
)


PASSWORD = "FAKE administrative passphrase 2026"
REPLACEMENT = "FAKE replacement administrative passphrase"


@contextmanager
def db_connection(path: Path):
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_nexo_v28(path: Path, *, historical_data: bool = True) -> None:
    with db_connection(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        migrate(connection)
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version != 28:
            raise AssertionError(f"fixture unexpectedly at schema {version}")
        if historical_data:
            connection.execute("INSERT INTO providers VALUES('p','FAKE provider','https://provider.invalid/v1','','fixture')")
            connection.execute("INSERT INTO models(id,provider_id,label) VALUES('m','p','FAKE model')")
            connection.execute("INSERT INTO notebooks VALUES('n','FAKE notebook','synthetic','fixture','fixture')")
            connection.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES('c','FAKE conversation','fixture','fixture')")
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) VALUES('msg','c','user','FAKE content','fixture')")
            connection.execute("CREATE TABLE user_preferences(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT NOT NULL)")
            connection.execute("INSERT INTO user_preferences VALUES('last_chat_model','FAKE model','fixture')")


def add_experimental_ownership_columns(path: Path) -> None:
    with db_connection(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("ALTER TABLE conversations ADD COLUMN owner_user_id TEXT REFERENCES auth_users(id) ON DELETE RESTRICT")
        connection.execute("ALTER TABLE notebooks ADD COLUMN owner_user_id TEXT REFERENCES auth_users(id) ON DELETE RESTRICT")


class AdministrativePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "admin.sqlite3"
        self.store = AdministrativePersistence(self.path, idle_ttl=5, absolute_ttl=20,
                                                max_attempts=2, lockout_seconds=5, failure_window=20)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_database_status_is_read_only(self):
        self.assertEqual(self.store.status(), {"initialized": False, "active_sessions": 0})
        self.assertFalse(self.path.exists())

    def test_fresh_temporary_database_starts_uninitialized_and_has_valid_constraints(self):
        self.store.initialize()
        self.assertEqual(self.store.status(now=1), {"initialized": False, "active_sessions": 0})
        self.assertEqual(self.store.verify(), {"integrity": "ok", "foreign_keys": True,
                                               "initialized": False, "schema_version": 1})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with db_connection(self.path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT version FROM auth_schema_version").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_user_permissions(user_id,permission) VALUES('missing','chat:read')")

    def test_nexo_v28_is_preserved_and_bootstrap_requires_explicit_ownership(self):
        create_nexo_v28(self.path)
        store = AdministrativePersistence(self.path)
        store.initialize()
        self.assertFalse(store.status(now=1)["initialized"])
        with self.assertRaises(OwnershipRequired):
            store.bootstrap_admin("admin", PASSWORD, now=1)
        self.assertEqual(store.status(now=1)["initialized"], False)
        with db_connection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 28)
            self.assertEqual(connection.execute("SELECT content FROM messages WHERE id='msg'").fetchone()[0], "FAKE content")

        add_experimental_ownership_columns(self.path)
        with self.assertRaises(OwnershipRequired):
            store.bootstrap_admin("admin", PASSWORD, now=1)
        user_id = store.bootstrap_admin("admin", PASSWORD, assign_historical=True, now=1)
        self.assertTrue(store.status(now=1)["initialized"])
        with db_connection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT owner_user_id FROM conversations WHERE id='c'").fetchone()[0], user_id)
            self.assertEqual(connection.execute("SELECT owner_user_id FROM notebooks WHERE id='n'").fetchone()[0], user_id)
            self.assertEqual(connection.execute("SELECT value FROM user_preferences WHERE key='last_chat_model'").fetchone()[0], "FAKE model")
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 28)
        self.assertEqual(store.verify()["foreign_keys"], True)
        backup = self.root / "nexo-v28-backup.sqlite3"
        restored = self.root / "nexo-v28-restored.sqlite3"
        backup_database(self.path, backup)
        restore_database(backup, restored)
        restored_store = AdministrativePersistence(restored)
        self.assertTrue(restored_store.status(now=2)["initialized"])
        with db_connection(restored) as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 28)
            self.assertEqual(connection.execute("SELECT content FROM messages WHERE id='msg'").fetchone()[0], "FAKE content")

    def test_v28_bootstrap_without_historical_rows_needs_owner_columns_but_no_assignment_consent(self):
        create_nexo_v28(self.path, historical_data=False)
        self.store.initialize()
        with self.assertRaises(OwnershipRequired):
            self.store.bootstrap_admin("admin", PASSWORD, now=1)
        add_experimental_ownership_columns(self.path)
        user_id = self.store.bootstrap_admin("admin", PASSWORD, now=1)
        self.assertTrue(user_id)
        self.assertTrue(self.store.status(now=1)["initialized"])

    def test_explicit_permissions_are_persisted_and_unknown_permission_is_denied(self):
        self.store.initialize()
        self.store.bootstrap_admin("Admin", PASSWORD, permissions=frozenset({"chat:read"}), now=1)
        session = self.store.login("admin", PASSWORD, now=2)
        self.assertEqual(self.store.authenticate(session.token, "chat:read", now=3).permissions,
                         frozenset({"chat:read"}))
        with self.assertRaisesRegex(AuthFailure, "permission_denied"):
            self.store.authenticate(session.token, "system:admin", now=4)
        with self.assertRaises(ValueError):
            self.store.bootstrap_admin("other", PASSWORD, permissions=frozenset({"*"}), now=5)

    def test_session_hashes_csrf_hashes_idle_expiry_and_revocation(self):
        self.store.initialize()
        self.store.bootstrap_admin("admin", PASSWORD, now=10)
        session = self.store.login("admin", PASSWORD, now=10)
        with db_connection(self.path) as connection:
            token_hash, csrf_hash = connection.execute("SELECT token_hash,csrf_hash FROM auth_sessions").fetchone()
        self.assertEqual(token_hash, hashlib.sha256(session.token.encode()).digest())
        self.assertEqual(csrf_hash, hashlib.sha256(session.csrf_token.encode()).digest())
        self.assertNotIn(PASSWORD.encode(), self.path.read_bytes())
        self.assertNotIn(session.token.encode(), self.path.read_bytes())
        self.assertNotIn(session.csrf_token.encode(), self.path.read_bytes())

        for csrf in (None, "FAKE wrong csrf"):
            with self.subTest(csrf=csrf), self.assertRaisesRegex(AuthFailure, "csrf_invalid"):
                self.store.authenticate(session.token, "chat:write", csrf_token=csrf, mutating=True, now=13)
        rotated_csrf = self.store.issue_csrf_token(session.token, now=13)
        with self.assertRaisesRegex(AuthFailure, "csrf_invalid"):
            self.store.authenticate(session.token, "chat:write", csrf_token=session.csrf_token,
                                    mutating=True, now=13)
        self.store.authenticate(session.token, "chat:write", csrf_token=rotated_csrf,
                                mutating=True, now=14)
        with db_connection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT idle_expires_at FROM auth_sessions").fetchone()[0], 19)
        with self.assertRaisesRegex(AuthFailure, "authentication_required"):
            self.store.authenticate(session.token, "chat:read", now=19)
        with self.assertRaisesRegex(AuthFailure, "authentication_required"):
            self.store.authenticate(session.token, "chat:read", now=20)

    def test_login_rate_limit_survives_adapter_restart_and_success_clears_it(self):
        self.store.initialize()
        self.store.bootstrap_admin("admin", PASSWORD, now=1)
        for now in (2, 3):
            with self.assertRaisesRegex(AuthFailure, "authentication_failed"):
                self.store.login("admin", "FAKE wrong password", now=now)
        restarted = AdministrativePersistence(self.path, idle_ttl=5, absolute_ttl=20,
                                               max_attempts=2, lockout_seconds=5, failure_window=20)
        event_count = len(restarted.security_events())
        with self.assertRaisesRegex(AuthFailure, "authentication_failed"):
            restarted.login("admin", PASSWORD, now=4)
        self.assertEqual(len(restarted.security_events()), event_count)
        session = restarted.login("admin", PASSWORD, now=8)
        self.assertTrue(session.token)
        with db_connection(self.path) as connection:
            self.assertEqual(tuple(connection.execute("SELECT failures,locked_until FROM auth_login_guard").fetchone()), (0, 0.0))

    def test_password_reset_revokes_sessions_and_is_atomic_on_audit_failure(self):
        self.store.initialize()
        self.store.bootstrap_admin("admin", PASSWORD, now=1)
        session = self.store.login("admin", PASSWORD, now=2)
        with db_connection(self.path) as connection:
            old_hash = connection.execute("SELECT password_hash FROM auth_users").fetchone()[0]
            connection.execute("""CREATE TRIGGER fail_password_reset BEFORE INSERT ON auth_security_events
                WHEN NEW.event_type='password_reset_local' BEGIN SELECT RAISE(ABORT,'FAKE injected'); END""")
        with self.assertRaises(PersistenceConflict) as raised:
            self.store.reset_password_local(REPLACEMENT, now=3)
        self.assertNotIn("FAKE", str(raised.exception))
        with db_connection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT password_hash FROM auth_users").fetchone()[0], old_hash)
            self.assertIsNone(connection.execute("SELECT revoked_at FROM auth_sessions").fetchone()[0])
            connection.execute("DROP TRIGGER fail_password_reset")
        self.store.authenticate(session.token, "chat:read", now=4)
        self.store.reset_password_local(REPLACEMENT, now=5)
        self.assertEqual(self.store.status(now=5)["active_sessions"], 0)
        with self.assertRaisesRegex(AuthFailure, "authentication_failed"):
            self.store.login("admin", PASSWORD, now=6)
        self.assertTrue(self.store.login("admin", REPLACEMENT, now=7).token)

    def test_individual_global_and_concurrent_revocation_are_idempotent(self):
        self.store.initialize()
        self.store.bootstrap_admin("admin", PASSWORD, now=1)
        one = self.store.login("admin", PASSWORD, now=2)
        two = self.store.login("admin", PASSWORD, now=3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(lambda _: self.store.revoke_session(one.token, now=4), range(4)))
        self.assertCountEqual(outcomes, [True, False, False, False])
        self.store.authenticate(two.token, "chat:read", now=5)
        self.assertEqual(self.store.revoke_all_sessions(now=6), 1)
        self.assertEqual(self.store.revoke_all_sessions(now=7), 0)
        with self.assertRaisesRegex(AuthFailure, "authentication_required"):
            self.store.authenticate(two.token, "chat:read", now=8)
        events = [event[0] for event in self.store.security_events()]
        self.assertEqual(events.count("session_revoked"), 1)
        self.assertEqual(events.count("all_sessions_revoked"), 1)

    def test_bootstrap_concurrency_has_one_winner(self):
        self.store.initialize()

        def attempt():
            try:
                return ("ok", self.store.bootstrap_admin("admin", PASSWORD, now=1))
            except PersistenceConflict:
                return ("conflict", None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual([result[0] for result in outcomes], ["ok", "conflict"])
        self.assertTrue(self.store.status(now=1)["initialized"])
        self.assertEqual(len(self.store.security_events()), 1)

    def test_repeated_bootstrap_is_rejected_without_changing_identity_grants_owners_or_sessions(self):
        create_nexo_v28(self.path)
        self.store.initialize()
        add_experimental_ownership_columns(self.path)
        user_id = self.store.bootstrap_admin("admin", PASSWORD, assign_historical=True, now=1)
        session = self.store.login("admin", PASSWORD, now=2)

        def snapshot():
            with db_connection(self.path) as connection:
                return (
                    tuple(connection.execute("SELECT id,username,password_hash,status FROM auth_users ORDER BY id")),
                    tuple(connection.execute("SELECT user_id,permission FROM auth_user_permissions ORDER BY user_id,permission")),
                    tuple(connection.execute("SELECT id,owner_user_id FROM conversations ORDER BY id")),
                    tuple(connection.execute("SELECT id,owner_user_id FROM notebooks ORDER BY id")),
                    tuple(connection.execute("SELECT token_hash,csrf_hash,user_id,revoked_at FROM auth_sessions ORDER BY token_hash")),
                    tuple(connection.execute("SELECT event_type,actor_user_id,created_at FROM auth_security_events ORDER BY id")),
                    tuple(connection.execute("SELECT initialized,bootstrapped_at FROM auth_state WHERE id=1")),
                )

        before = snapshot()
        with self.assertRaises(PersistenceConflict):
            self.store.bootstrap_admin("second-admin", REPLACEMENT, assign_historical=True, now=3)
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.store.authenticate(session.token, "chat:read", now=4).subject, "admin")
        self.assertEqual(user_id, before[0][0][0])

    def test_bootstrap_event_failure_rolls_back_identity_permissions_and_ownership(self):
        create_nexo_v28(self.path)
        self.store.initialize()
        add_experimental_ownership_columns(self.path)
        with db_connection(self.path) as connection:
            connection.execute("""CREATE TRIGGER fail_bootstrap BEFORE INSERT ON auth_security_events
                WHEN NEW.event_type='bootstrap_completed' BEGIN SELECT RAISE(ABORT,'FAKE injected'); END""")
        with self.assertRaises(PersistenceConflict):
            self.store.bootstrap_admin("admin", PASSWORD, assign_historical=True, now=1)
        with db_connection(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_user_permissions").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT owner_user_id FROM conversations WHERE id='c'").fetchone()[0])
            self.assertEqual(connection.execute("SELECT initialized FROM auth_state").fetchone()[0], 0)
            connection.execute("DROP TRIGGER fail_bootstrap")
        reopened = AdministrativePersistence(self.path)
        self.assertEqual(reopened.status(now=2), {"initialized": False, "active_sessions": 0})
        with self.assertRaisesRegex(AuthFailure, "authentication_failed"):
            reopened.login("admin", PASSWORD, now=2)
        self.assertEqual(reopened.verify()["initialized"], False)
        reopened.bootstrap_admin("admin", PASSWORD, assign_historical=True, now=3)
        self.assertTrue(reopened.status(now=3)["initialized"])

    def test_reset_and_session_creation_fail_closed_when_database_is_locked(self):
        self.store.initialize()
        self.store.bootstrap_admin("admin", PASSWORD, now=1)
        lock = sqlite3.connect(self.path, isolation_level=None, timeout=0.1)
        lock.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(PersistenceUnavailable):
                self.store.login("admin", PASSWORD, now=2)
        finally:
            lock.rollback()
            lock.close()
        self.assertTrue(self.store.login("admin", PASSWORD, now=3).token)

    def test_partial_incompatible_and_unknown_schema_fail_without_completion(self):
        self.path.touch()
        with db_connection(self.path) as connection:
            connection.execute("CREATE TABLE auth_state(id INTEGER PRIMARY KEY)")
        with self.assertRaises(IncompatibleSchema):
            self.store.initialize()
        with db_connection(self.path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"auth_state"})

        other = self.root / "future.sqlite3"
        create_nexo_v28(other, historical_data=False)
        with db_connection(other) as connection:
            connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(29,'fixture')")
        with self.assertRaises(IncompatibleSchema):
            AdministrativePersistence(other).initialize()

    def test_legacy_auth_core_schema_is_rejected_without_rewriting_it(self):
        legacy_path = self.root / "legacy-auth.sqlite3"
        legacy = AdminAuthStore(legacy_path)
        legacy.initialize()
        with db_connection(legacy_path) as connection:
            before = tuple(sorted(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")))
        with self.assertRaises(IncompatibleSchema):
            AdministrativePersistence(legacy_path).initialize()
        with db_connection(legacy_path) as connection:
            after = tuple(sorted(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")))
        self.assertEqual(after, before)
        self.assertIn("auth_admin", after)
        self.assertNotIn("auth_users", after)

    def test_incomplete_v28_ledger_and_foreign_key_orphans_are_rejected(self):
        incomplete = self.root / "incomplete.sqlite3"
        with db_connection(incomplete) as connection:
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
            connection.executemany("INSERT INTO schema_migrations VALUES(?, 'fixture')", ((i,) for i in range(1, 29)))
        with self.assertRaises(IncompatibleSchema):
            AdministrativePersistence(incomplete).initialize()

        orphan = self.root / "orphan.sqlite3"
        create_nexo_v28(orphan, historical_data=False)
        with db_connection(orphan) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) VALUES('orphan','missing','user','FAKE','fixture')")
        with self.assertRaises(IncompatibleSchema):
            AdministrativePersistence(orphan).initialize()
        with db_connection(orphan) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='auth_users'").fetchone()[0], 0)

    def test_state_inconsistencies_and_unknown_admin_schema_version_are_rejected(self):
        self.store.initialize()
        with db_connection(self.path) as connection:
            connection.execute("INSERT INTO auth_users VALUES('u','admin','admin','FAKE hash','active',1,1)")
        with self.assertRaises(IncompatibleSchema):
            self.store.status(now=2)

        initialized_path = self.root / "inconsistent-initialized.sqlite3"
        initialized_store = AdministrativePersistence(initialized_path)
        initialized_store.initialize()
        with db_connection(initialized_path) as connection:
            connection.execute("UPDATE auth_state SET initialized=1,bootstrapped_at=1 WHERE id=1")
        with self.assertRaises(IncompatibleSchema):
            initialized_store.status(now=2)

        state_path = self.root / "unknown-admin-version.sqlite3"
        store = AdministrativePersistence(state_path)
        store.initialize()
        with db_connection(state_path) as connection:
            connection.execute("UPDATE auth_schema_version SET version=99 WHERE id=1")
        with self.assertRaises(IncompatibleSchema):
            store.verify()

        domain_path = self.root / "changed-domain-version.sqlite3"
        domain_store = AdministrativePersistence(domain_path)
        domain_store.initialize()
        domain_store.bootstrap_admin("admin", PASSWORD, now=1)
        with db_connection(domain_path) as connection:
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
            connection.execute("INSERT INTO schema_migrations VALUES(29,'fixture')")
        with self.assertRaises(IncompatibleSchema):
            domain_store.login("admin", PASSWORD, now=2)

    def test_production_paths_and_symlinks_are_rejected(self):
        with self.assertRaises(ValueError):
            AdministrativePersistence(self.root / "nexo.sqlite3")
        with self.assertRaises(ValueError):
            AdministrativePersistence(Path("/var/tmp/not-a-fixture.sqlite3"))
        target = self.root / "real.sqlite3"
        target.touch()
        alias = self.root / "alias.sqlite3"
        alias.symlink_to(target)
        with self.assertRaises(ValueError):
            AdministrativePersistence(alias)
        real_directory = self.root / "real-directory"
        real_directory.mkdir()
        directory_alias = self.root / "directory-alias"
        directory_alias.symlink_to(real_directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            AdministrativePersistence(directory_alias / "database.sqlite3")
        configured_data_dir = self.root / "configured-data"
        configured_data_dir.mkdir()
        product_named_fixture = configured_data_dir / "custom-name.sqlite3"
        with patch.dict(os.environ, {"NEXO_DATA_DIR": str(configured_data_dir)}):
            with self.assertRaises(ValueError):
                AdministrativePersistence(product_named_fixture)
            with self.assertRaises(ValueError):
                backup_database(product_named_fixture, self.root / "unwanted-backup.sqlite3")

    def test_sqlite_backup_and_restore_preserve_wal_rows_and_foreign_keys(self):
        self.store.initialize()
        self.store.bootstrap_admin("admin", PASSWORD, now=1)
        session = self.store.login("admin", PASSWORD, now=2)
        source = sqlite3.connect(self.path, isolation_level=None)
        source.execute("PRAGMA journal_mode=WAL")
        actor = source.execute("SELECT actor_user_id FROM auth_security_events WHERE event_type='bootstrap_completed'").fetchone()[0]
        source.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) VALUES('login_succeeded',?,3)", (actor,))
        self.assertTrue(Path(str(self.path) + "-wal").exists())
        backup = self.root / "snapshot.sqlite3"
        restored = self.root / "restored.sqlite3"
        self.assertEqual(backup_database(self.path, backup), {"integrity": "ok", "foreign_key_violations": 0})
        self.assertEqual(restore_database(backup, restored), {"integrity": "ok", "foreign_key_violations": 0})
        source.close()
        with db_connection(restored) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT MAX(version) FROM auth_schema_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_security_events WHERE event_type='login_succeeded' AND created_at=3").fetchone()[0], 1)
        restored_store = AdministrativePersistence(restored)
        self.assertEqual(restored_store.status(now=3)["active_sessions"], 1)
        self.assertTrue(restored_store.authenticate(session.token, "chat:read", now=3))

    def test_backup_during_controlled_wal_writer_commit_is_transactionally_consistent(self):
        source = self.root / "concurrent-wal.sqlite3"
        backup = self.root / "concurrent-wal-backup.sqlite3"
        row_count = 900
        payload_size = 2048
        with db_connection(source) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE snapshot_state(generation INTEGER PRIMARY KEY)")
            connection.execute("""CREATE TABLE snapshot_rows(
                generation INTEGER NOT NULL REFERENCES snapshot_state(generation) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(generation,ordinal))""")
            connection.execute("INSERT INTO snapshot_state VALUES(1)")
            connection.executemany("INSERT INTO snapshot_rows VALUES(1,?,?)",
                                   ((i, "g1:" + "a" * payload_size) for i in range(row_count)))

        writer_finished = threading.Event()
        progress_at_write = []

        def writer_transaction():
            connection = sqlite3.connect(source, timeout=5, isolation_level=None)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO snapshot_state VALUES(2)")
                connection.executemany("INSERT INTO snapshot_rows VALUES(2,?,?)",
                                       ((i, "g2:" + "b" * payload_size) for i in range(row_count)))
                connection.execute("DELETE FROM snapshot_state WHERE generation=1")
                connection.commit()
            finally:
                connection.close()
                writer_finished.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            writer_future = None

            def progress(_status, remaining, total):
                nonlocal writer_future
                if writer_future is None:
                    progress_at_write.append((remaining, total))
                    writer_future = pool.submit(writer_transaction)
                    writer_future.result(timeout=10)

            result = backup_database(source, backup, pages=8, progress=progress)
            self.assertIsNotNone(writer_future)
            writer_future.result(timeout=10)

        self.assertTrue(writer_finished.is_set())
        self.assertTrue(progress_at_write[0][0] > 0)
        self.assertEqual(result, {"integrity": "ok", "foreign_key_violations": 0})
        with db_connection(backup) as connection:
            states = [row[0] for row in connection.execute("SELECT generation FROM snapshot_state ORDER BY generation")]
            grouped = connection.execute("""SELECT generation,COUNT(*),MIN(payload),MAX(payload)
                FROM snapshot_rows GROUP BY generation ORDER BY generation""").fetchall()
            self.assertEqual(len(states), 1)
            self.assertEqual(len(grouped), 1)
            generation, count, first_payload, last_payload = grouped[0]
            self.assertEqual(states, [generation])
            self.assertEqual(count, row_count)
            self.assertEqual(first_payload[:3], f"g{generation}:")
            self.assertEqual(last_payload[:3], f"g{generation}:")

    def test_backup_and_restore_failures_remove_staging_and_never_publish_partial_files(self):
        self.store.initialize()
        with db_connection(self.path) as connection:
            connection.execute("CREATE TABLE payload(value TEXT)")
            connection.executemany("INSERT INTO payload VALUES(?)", (("FAKE" + "x" * 2000,) for _ in range(100)))
        backup = self.root / "failed-backup.sqlite3"
        restore = self.root / "failed-restore.sqlite3"

        def fail_after_copy(_status, remaining, total):
            if remaining < total:
                raise RuntimeError("FAKE injected copy failure")

        with self.assertRaises(BackupFailure):
            backup_database(self.path, backup, pages=1, progress=fail_after_copy)
        self.assertFalse(backup.exists())
        self.assertFalse(list(self.root.glob(".*.partial")))

        complete_backup = self.root / "complete.sqlite3"
        backup_database(self.path, complete_backup)
        with self.assertRaises(BackupFailure):
            restore_database(complete_backup, restore, pages=1, progress=fail_after_copy)
        self.assertFalse(restore.exists())
        self.assertFalse(list(self.root.glob(".*.partial")))
        self.assertEqual(verify_sqlite_file(complete_backup)["integrity"], "ok")

    def test_corrupt_backup_and_existing_destination_are_rejected_without_overwrite(self):
        self.store.initialize()
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"FAKE not sqlite")
        destination = self.root / "restored.sqlite3"
        with self.assertRaises(BackupFailure):
            restore_database(corrupt, destination)
        self.assertFalse(destination.exists())

        with db_connection(self.path) as connection:
            connection.execute("CREATE TABLE payload(value TEXT)")
        destination.write_bytes(b"FAKE keep existing")
        with self.assertRaises(BackupFailure):
            backup_database(self.path, destination)
        self.assertEqual(destination.read_bytes(), b"FAKE keep existing")


if __name__ == "__main__":
    unittest.main()
