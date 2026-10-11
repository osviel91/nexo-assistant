import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prototypes.admin_auth import AdminAuthStore, AuthFailure, AuthUnavailable
from prototypes.admin_auth_cli import main


PASSWORD = "FAKE CLI passphrase for tests 2026"
REPLACEMENT = "FAKE replacement CLI passphrase 2026"


class TTY(io.StringIO):
    def isatty(self):
        return True


class AdminAuthCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "private" / "admin-auth.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, command, *, interactive=False, inputs=(), passwords=(), extra_env=None):
        stdout, stderr = io.StringIO(), TTY() if interactive else io.StringIO()
        stdin = TTY() if interactive else io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch("sys.stdin", stdin), \
             patch("builtins.input", side_effect=inputs), \
             patch("prototypes.admin_auth.getpass", side_effect=passwords), \
             patch.dict(os.environ, extra_env or {}, clear=False):
            code = main(["--db", str(self.database), command])
        return code, stdout.getvalue(), stderr.getvalue()

    def bootstrap(self):
        code, _, error = self.invoke("bootstrap", interactive=True,
                                     inputs=("admin",), passwords=(PASSWORD, PASSWORD))
        self.assertEqual((code, error), (0, ""))

    def test_status_is_read_only_and_redacts_identity_and_credentials(self):
        code, output, error = self.invoke("status")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.strip(), "state=uninitialized active_sessions=0")
        self.assertFalse(self.database.exists())

        self.bootstrap()
        auth = AdminAuthStore(self.database)
        session = auth.login("admin", PASSWORD)
        code, output, error = self.invoke("status")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("state=initialized active_sessions=1", output)
        for secret in ("admin", PASSWORD, session.token, session.csrf_token):
            self.assertNotIn(secret, output + error)

    def test_bootstrap_is_tty_only_one_time_and_database_is_private(self):
        code, output, error = self.invoke("bootstrap", interactive=False)
        self.assertEqual(code, 2)
        self.assertIn("TTY", error)
        self.assertFalse(self.database.parent.exists())
        self.assertFalse(self.database.exists())

        self.bootstrap()
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)
        code, output, error = self.invoke("bootstrap", interactive=True,
                                          passwords=("FAKE should not be prompted",))
        self.assertEqual(code, 4)
        self.assertNotIn("FAKE", output + error)

    def test_reset_password_is_local_and_revokes_all_existing_sessions(self):
        self.bootstrap()
        auth = AdminAuthStore(self.database)
        session = auth.login("admin", PASSWORD)
        code, output, error = self.invoke("reset-password", interactive=True,
                                          passwords=(REPLACEMENT, REPLACEMENT))
        self.assertEqual((code, output.strip(), error), (0, "password_reset_sessions_revoked", ""))
        self.assertEqual(auth.session_count(), 0)
        with self.assertRaises(AuthFailure):
            auth.authenticate(session.token, "chat:read")
        self.assertTrue(auth.login("admin", REPLACEMENT).token)
        self.assertNotIn(REPLACEMENT, output + error)

    def test_reset_rolls_back_password_and_sessions_if_a_later_write_fails(self):
        self.bootstrap()
        auth = AdminAuthStore(self.database)
        session = auth.login("admin", PASSWORD)
        connection = sqlite3.connect(self.database)
        try:
            original_hash = connection.execute("SELECT password_hash FROM auth_admin WHERE id=1").fetchone()[0]
            connection.execute("""CREATE TRIGGER fail_reset_event BEFORE INSERT ON auth_security_events
                WHEN NEW.event_type='password_reset_local'
                BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(AuthUnavailable):
            auth.reset_password_local(REPLACEMENT, now=10)

        connection = sqlite3.connect(self.database)
        try:
            current_hash = connection.execute("SELECT password_hash FROM auth_admin WHERE id=1").fetchone()[0]
            session_revocation = connection.execute("SELECT revoked_at FROM auth_sessions").fetchone()[0]
            connection.execute("DROP TRIGGER fail_reset_event")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(current_hash, original_hash)
        self.assertIsNone(session_revocation)
        auth.authenticate(session.token, "chat:read")
        with self.assertRaises(AuthFailure):
            auth.login("admin", REPLACEMENT)

    def test_reset_mismatch_changes_nothing_and_non_tty_is_rejected(self):
        self.bootstrap()
        auth = AdminAuthStore(self.database)
        session = auth.login("admin", PASSWORD)
        code, _, error = self.invoke("reset-password", interactive=True,
                                     passwords=(REPLACEMENT, "FAKE mismatch"))
        self.assertEqual(code, 2)
        self.assertNotIn("FAKE", error)
        auth.authenticate(session.token, "chat:read")
        with self.assertRaises(AuthFailure):
            auth.login("admin", REPLACEMENT)

        code, _, error = self.invoke("reset-password", interactive=False)
        self.assertEqual(code, 2)
        self.assertIn("TTY", error)

    def test_revoke_requires_tty_confirmation_and_invalidates_sessions(self):
        self.bootstrap()
        auth = AdminAuthStore(self.database)
        session = auth.login("admin", PASSWORD)
        code, _, error = self.invoke("revoke-sessions", interactive=True, inputs=("no",))
        self.assertEqual(code, 2)
        self.assertNotIn("FAKE", error)
        auth.authenticate(session.token, "chat:read")

        code, output, error = self.invoke("revoke-sessions", interactive=True, inputs=("REVOKE",))
        self.assertEqual((code, output.strip(), error), (0, "sessions_revoked", ""))
        with self.assertRaises(AuthFailure):
            auth.authenticate(session.token, "chat:read")

    def test_unsafe_database_targets_and_directory_permissions_are_rejected(self):
        app_data = self.root / "nexo-data"
        app_data.mkdir(mode=0o700)
        prohibited = app_data / "nexo.sqlite3"
        with patch.dict(os.environ, {"NEXO_DATA_DIR": str(app_data)}):
            code, output, error = main_for_path(prohibited, "status")
        self.assertEqual(code, 2)
        self.assertNotIn(str(prohibited), output + error)
        self.assertFalse(prohibited.exists())

        alias = self.root / "auth-alias.sqlite3"
        alias.symlink_to(prohibited)
        with patch.dict(os.environ, {"NEXO_DATA_DIR": str(app_data)}):
            code, output, error = main_for_path(alias, "status")
        self.assertEqual(code, 2)
        self.assertFalse(prohibited.exists())
        self.assertNotIn(str(prohibited), output + error)

        unsafe = self.root / "world-writable"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        path = unsafe / "admin-auth.sqlite3"
        code, _, error = main_for_path(path, "bootstrap", interactive=True,
                                       inputs=("admin",), passwords=(PASSWORD, PASSWORD))
        self.assertEqual(code, 2)
        self.assertFalse(path.exists())
        self.assertNotIn(PASSWORD, error)

    def test_corrupt_or_unavailable_storage_fails_closed_without_detail(self):
        self.database.parent.mkdir(mode=0o700)
        self.database.write_bytes(b"not a sqlite database")
        code, output, error = self.invoke("status")
        self.assertEqual(code, 3)
        self.assertNotIn("sqlite", error.lower())
        self.assertNotIn(str(self.database), output + error)


def main_for_path(database, command, *, interactive=False, inputs=(), passwords=()):
    stdout, stderr = io.StringIO(), TTY() if interactive else io.StringIO()
    stdin = TTY() if interactive else io.StringIO()
    with patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch("sys.stdin", stdin), \
         patch("builtins.input", side_effect=inputs), \
         patch("prototypes.admin_auth.getpass", side_effect=passwords):
        code = main(["--db", str(database), command])
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
