import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.admin_store import (
    CP2_B2_MINIMUM_PERMISSIONS,
    AdminSchemaUnavailable,
    AdminStorageUnavailable,
    AdminStore,
    AuthenticationFailure,
    BootstrapBlocked,
    BootstrapUnavailable,
)
from app.safe_migrations import CURRENT_SCHEMA_VERSION, JOURNAL_NAME, DatabaseLease, SafeMigrationEngine


class AdminStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "nexo.sqlite3"
        result = SafeMigrationEngine(self.directory.name).migrate()
        self.assertEqual((result.target_version, result.changed), (CURRENT_SCHEMA_VERSION, True))

    def tearDown(self):
        self.directory.cleanup()

    def lease(self, exclusive=True):
        return DatabaseLease(self.database, exclusive=exclusive)

    def add_legacy_preference_table(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL)")
            connection.execute("INSERT INTO user_preferences VALUES('last_chat_model','provider::model','2026-10-14T00:00:00Z')")

    def test_bootstrap_uses_v31_tables_and_copies_preferences(self):
        self.add_legacy_preference_table()
        with self.lease() as lease:
            store = AdminStore(self.database, lease)
            self.assertEqual(store.status(), {"state": "schema_installed", "active_sessions": 0})
            with self.assertRaises(AuthenticationFailure):
                store.login("admin", "a sufficiently long passphrase")
            with self.assertRaisesRegex(ValueError, "invalid explicit permissions"):
                store.bootstrap_admin("admin", "a sufficiently long passphrase", permissions=frozenset({"providers:write"}))
            with sqlite3.connect(self.database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_security_events").fetchone()[0], 0)
            user_id = store.bootstrap_admin("Admin@Example.com", "a sufficiently long passphrase")
            self.assertEqual(store.status()["state"], "bootstrap_completed")
            with sqlite3.connect(self.database) as connection:
                self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 31)
                self.assertEqual(connection.execute("SELECT state FROM auth_state WHERE id=1").fetchone()[0], "bootstrap_completed")
                self.assertEqual(connection.execute("SELECT actor_user_id FROM auth_security_events WHERE event_type='bootstrap_completed'").fetchone()[0], user_id)
                self.assertEqual(set(row[0] for row in connection.execute("SELECT permission FROM auth_user_permissions WHERE user_id=?", (user_id,))), CP2_B2_MINIMUM_PERMISSIONS)
                self.assertEqual(connection.execute("SELECT value FROM auth_user_preferences WHERE user_id=? AND key='last_chat_model'", (user_id,)).fetchone()[0], "provider::model")
                password_hash = connection.execute("SELECT password_hash FROM auth_users WHERE id=?", (user_id,)).fetchone()[0]
                self.assertNotIn("a sufficiently long passphrase", password_hash)

    def test_bootstrap_is_exclusive_and_refuses_existing_conversation_data(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES('c','old','1','1')")
        with self.lease(exclusive=False) as shared_lease:
            with self.assertRaises(BootstrapUnavailable):
                AdminStore(self.database, shared_lease).bootstrap_admin("admin", "a sufficiently long passphrase")
        with self.lease() as lease:
            store = AdminStore(self.database, lease)
            with self.assertRaises(BootstrapBlocked):
                store.bootstrap_admin("admin", "a sufficiently long passphrase")
            self.assertEqual(store.status()["state"], "schema_installed")
            with sqlite3.connect(self.database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)

    def test_bootstrap_failure_rolls_back_identity_grants_and_state(self):
        with self.lease() as lease:
            store = AdminStore(self.database, lease)
            with patch.object(store, "_event", side_effect=RuntimeError("injected failure")):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    store.bootstrap_admin("admin", "a sufficiently long passphrase")
            self.assertEqual(store.status()["state"], "schema_installed")
            with sqlite3.connect(self.database) as connection:
                for table in ("auth_users", "auth_user_permissions", "auth_security_events"):
                    self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_login_csrf_permissions_idle_expiry_and_revocation(self):
        now = [1000.0]
        with self.lease() as lease:
            store = AdminStore(self.database, lease, idle_ttl=20, absolute_ttl=60, clock=lambda: now[0])
            user_id = store.bootstrap_admin("admin", "a sufficiently long passphrase")
            session = store.login("ADMIN", "a sufficiently long passphrase")
            principal = store.authenticate(session.token, "config:read")
            self.assertEqual((principal.user_id, principal.username), (user_id, "admin"))
            self.assertEqual(principal.permissions, CP2_B2_MINIMUM_PERMISSIONS)
            with self.assertRaises(AuthenticationFailure) as denied:
                store.authenticate(session.token, "not-a-grant")
            self.assertEqual(denied.exception.code, "permission_denied")
            with self.assertRaises(AuthenticationFailure) as csrf:
                store.authenticate(session.token, "config:write", mutating=True, csrf_token="wrong")
            self.assertEqual(csrf.exception.code, "csrf_invalid")
            store.authenticate(session.token, "config:write", mutating=True, csrf_token=session.csrf_token)
            next_csrf = store.rotate_csrf_token(session.token)
            with self.assertRaises(AuthenticationFailure):
                store.authenticate(session.token, "config:write", mutating=True, csrf_token=session.csrf_token)
            store.authenticate(session.token, "config:write", mutating=True, csrf_token=next_csrf)
            revoked = store.login("admin", "a sufficiently long passphrase")
            self.assertTrue(store.logout(revoked.token))
            self.assertFalse(store.logout(revoked.token))
            with self.assertRaises(AuthenticationFailure):
                store.authenticate(revoked.token, "config:read")
            with sqlite3.connect(self.database) as connection:
                token_hash, csrf_hash = connection.execute("SELECT token_hash,csrf_hash FROM auth_sessions").fetchone()
                self.assertEqual(token_hash, hashlib.sha256(session.token.encode()).digest())
                self.assertEqual(csrf_hash, hashlib.sha256(next_csrf.encode()).digest())
                self.assertNotIn(session.token.encode(), Path(self.database).read_bytes())
            now[0] += 21
            with self.assertRaises(AuthenticationFailure) as expired:
                store.authenticate(session.token, "config:read")
            self.assertEqual(expired.exception.code, "authentication_required")
            self.assertFalse(store.logout(session.token))

    def test_revoke_all_and_concurrent_bootstrap_has_one_winner(self):
        with self.lease() as lease:
            store = AdminStore(self.database, lease)
            barrier = threading.Barrier(2)

            def bootstrap():
                barrier.wait()
                try:
                    return store.bootstrap_admin("admin", "a sufficiently long passphrase")
                except BootstrapUnavailable:
                    return None

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: bootstrap(), range(2)))
            user_id = next(result for result in results if result is not None)
            self.assertEqual(sum(result is not None for result in results), 1)
            first = store.login("admin", "a sufficiently long passphrase")
            second = store.login("admin", "a sufficiently long passphrase")
            self.assertEqual(store.revoke_all_sessions(user_id), 2)
            self.assertEqual(store.revoke_all_sessions(user_id), 0)
            with self.assertRaises(AuthenticationFailure):
                store.authenticate(first.token, "config:read")
            with self.assertRaises(AuthenticationFailure):
                store.authenticate(second.token, "config:read")

    def test_login_lockout_is_persistent_and_expires(self):
        now = [1000.0]
        with self.lease() as lease:
            store = AdminStore(self.database, lease, max_attempts=2, lockout_seconds=10,
                               failure_window=30, clock=lambda: now[0])
            store.bootstrap_admin("admin", "a sufficiently long passphrase")
            for _ in range(2):
                with self.assertRaises(AuthenticationFailure):
                    store.login("admin", "wrong passphrase")
            with self.assertRaises(AuthenticationFailure):
                store.login("admin", "a sufficiently long passphrase")
            with sqlite3.connect(self.database) as connection:
                self.assertEqual(connection.execute("SELECT failures,locked_until FROM auth_login_guard WHERE subject='administrator'").fetchone(), (2, 1010.0))
            now[0] = 1011.0
            self.assertTrue(store.login("admin", "a sufficiently long passphrase").token)
            with sqlite3.connect(self.database) as connection:
                self.assertEqual(connection.execute("SELECT failures,locked_until FROM auth_login_guard WHERE subject='administrator'").fetchone(), (0, 0.0))

    def test_rejects_non_v31_schema_and_released_lease(self):
        with self.lease() as lease:
            store = AdminStore(self.database, lease)
            with sqlite3.connect(self.database) as connection:
                connection.execute("DELETE FROM schema_migrations WHERE version=31")
            with self.assertRaises(AdminSchemaUnavailable):
                store.status()
        lease.close()
        with self.assertRaises(AdminStorageUnavailable):
            store.status()

    def test_does_not_create_missing_database_and_preserves_recovery_journal(self):
        missing = Path(self.directory.name) / "missing" / "nexo.sqlite3"
        missing.parent.mkdir()
        with DatabaseLease(missing, exclusive=True) as lease:
            with self.assertRaises(AdminStorageUnavailable):
                AdminStore(missing, lease).status()
            self.assertFalse(missing.exists())
        journal = Path(self.directory.name) / JOURNAL_NAME
        journal.write_text("preserve recovery evidence")
        with self.lease() as lease:
            with self.assertRaises(AdminSchemaUnavailable):
                AdminStore(self.database, lease).status()
        self.assertEqual(journal.read_text(), "preserve recovery evidence")

    def test_login_waits_for_runtime_writer_instead_of_rejecting_rollback_journal(self):
        with self.lease(exclusive=True) as bootstrap_lease:
            AdminStore(self.database, bootstrap_lease).bootstrap_admin("admin", "a sufficiently long passphrase")
        with self.lease(exclusive=False) as runtime_lease:
            store = AdminStore(self.database, runtime_lease)
            writer = sqlite3.connect(self.database, timeout=5, isolation_level=None)
            try:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE agent_runtime_settings SET max_tool_calls=max_tool_calls+1 WHERE id=1")
                self.assertTrue(Path(str(self.database) + "-journal").exists())
                with store._connection() as connection:
                    self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(store.login, "admin", "a sufficiently long passphrase")
                    time.sleep(0.1)
                    writer.commit()
                    self.assertTrue(pending.result(timeout=5).token)
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main()
