import concurrent.futures
import logging
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from prototypes.admin_auth import (
    ADMIN_PERMISSIONS,
    AdminAuthStore,
    AuthFailure,
    AuthUnavailable,
    BootstrapComplete,
    authorize,
    bootstrap_from_console,
    service_principal,
)
from prototypes.admin_auth_http import COOKIE_NAME, create_auth_router, require_permission


ORIGIN = "https://nexo.example.test"
ADMIN = "FAKE admin passphrase for tests 2026"


class AdminAuthCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "auth.sqlite3"
        self.auth = AdminAuthStore(self.path, idle_ttl=10, absolute_ttl=30,
                                   max_attempts=2, lockout_seconds=5, failure_window=20)
        self.auth.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def bootstrap(self):
        self.auth.bootstrap_local("admin", ADMIN, now=1)

    def login(self, now=2):
        return self.auth.login("admin", ADMIN, now=now)

    def test_bootstrap_is_one_time_and_password_hash_is_versioned(self):
        self.bootstrap()
        with self.assertRaises(BootstrapComplete):
            self.auth.bootstrap_local("second", ADMIN, now=2)
        connection = sqlite3.connect(self.path)
        try:
            algorithm, version = connection.execute("SELECT password_hash FROM auth_admin").fetchone()[0].split("$")[:2]
        finally:
            connection.close()
        self.assertEqual((algorithm, version), ("scrypt", "1"))
        self.assertEqual(self.auth.security_events(), [("bootstrap_completed", 1.0)])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_console_bootstrap_confirms_password_without_http_surface(self):
        with patch("builtins.input", return_value="admin"), patch(
                "prototypes.admin_auth.getpass", side_effect=[ADMIN, ADMIN]):
            bootstrap_from_console(self.auth)
        self.assertTrue(self.auth.login("admin", ADMIN).token)

        other = AdminAuthStore(self.path.parent / "other-auth.sqlite3")
        other.initialize()
        with patch("builtins.input", return_value="admin"), patch(
                "prototypes.admin_auth.getpass", side_effect=[ADMIN, "FAKE mismatch"]):
            with self.assertRaisesRegex(ValueError, "do not match"):
                bootstrap_from_console(other)
        with self.assertRaises(AuthFailure):
            other.login("admin", ADMIN, now=2)

    def test_bad_password_rate_limit_and_recovery_after_lockout(self):
        self.bootstrap()
        for timestamp in (2, 3):
            with self.assertRaisesRegex(AuthFailure, "authentication_failed"):
                self.auth.login("admin", "wrong fake password", now=timestamp)
        with self.assertRaisesRegex(AuthFailure, "authentication_failed"):
            self.auth.login("admin", ADMIN, now=7)
        session = self.login(now=8)
        self.assertTrue(session.token)
        self.assertIn(("login_failed", 3.0), self.auth.security_events())

    def test_rate_limit_cannot_be_evaded_by_changing_username(self):
        self.bootstrap()
        for username, timestamp in (("admin", 100), ("attacker", 101)):
            with self.assertRaisesRegex(AuthFailure, "authentication_failed"):
                self.auth.login(username, "FAKE wrong password", now=timestamp)
        with self.assertRaisesRegex(AuthFailure, "authentication_failed"):
            self.auth.login("admin", ADMIN, now=102)
        self.assertTrue(self.auth.login("admin", ADMIN, now=107).token)

    def test_login_logout_and_session_persist_across_store_restart(self):
        self.bootstrap()
        session = self.login()
        restarted = AdminAuthStore(self.path, idle_ttl=10, absolute_ttl=30,
                                   max_attempts=2, lockout_seconds=5, failure_window=20)
        principal = restarted.authenticate(session.token, "chat:read", now=3)
        self.assertEqual((principal.kind, principal.subject), ("user", "admin"))
        restarted.logout(session.token, now=4)
        with self.assertRaisesRegex(AuthFailure, "authentication_required"):
            self.auth.authenticate(session.token, "chat:read", now=5)

    def test_idle_expiry_slides_but_absolute_expiry_does_not(self):
        self.bootstrap()
        session = self.login(now=10)
        self.auth.authenticate(session.token, "chat:read", now=19)
        with self.assertRaisesRegex(AuthFailure, "authentication_required"):
            self.auth.authenticate(session.token, "chat:read", now=29)

        absolute = AdminAuthStore(self.path, idle_ttl=8, absolute_ttl=10,
                                  max_attempts=2, lockout_seconds=5, failure_window=20)
        session = absolute.login("admin", ADMIN, now=40)
        absolute.authenticate(session.token, "chat:read", now=47)
        with self.assertRaisesRegex(AuthFailure, "authentication_required"):
            absolute.authenticate(session.token, "chat:read", now=50)
        self.assertIn(("session_expired", 50.0), self.auth.security_events())

    def test_csrf_absent_invalid_and_valid_and_permission_deny_by_default(self):
        self.bootstrap()
        session = self.login()
        for csrf in (None, "wrong"):
            with self.subTest(csrf=csrf), self.assertRaisesRegex(AuthFailure, "csrf_invalid"):
                self.auth.authenticate(session.token, "chat:write", now=3,
                                       csrf_token=csrf, mutating=True)
        principal = self.auth.authenticate(session.token, "chat:write", now=3,
                                           csrf_token=session.csrf_token, mutating=True)
        self.assertEqual(principal.permissions, ADMIN_PERMISSIONS)
        with self.assertRaisesRegex(AuthFailure, "permission_denied"):
            self.auth.authenticate(session.token, "unlisted:permission", now=4)

    def test_individual_global_and_password_reset_revocation(self):
        self.bootstrap()
        one, two = self.login(), self.auth.login("admin", ADMIN, now=3)
        self.auth.revoke_session(one.token, now=4)
        self.auth.authenticate(two.token, "chat:read", now=5)
        self.auth.revoke_all(now=6)
        with self.assertRaises(AuthFailure):
            self.auth.authenticate(two.token, "chat:read", now=7)
        three = self.auth.login("admin", ADMIN, now=8)
        self.auth.reset_password_local("FAKE replacement passphrase 2026", now=9)
        self.assertEqual(self.auth.session_count(), 0)
        with self.assertRaises(AuthFailure):
            self.auth.authenticate(three.token, "chat:read", now=10)
        with self.assertRaises(AuthFailure):
            self.auth.login("admin", ADMIN, now=10)

    def test_storage_failure_fails_closed_without_sqlite_details(self):
        self.bootstrap()
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(AuthUnavailable) as raised:
            self.auth.login("admin", ADMIN, now=2)
        self.assertEqual(str(raised.exception), "authentication_unavailable")

    def test_concurrent_bootstrap_and_login_are_serialized(self):
        outcomes = []

        def bootstrap():
            try:
                self.auth.bootstrap_local("admin", ADMIN, now=1)
                return "created"
            except BootstrapComplete:
                return "already-created"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: bootstrap(), range(2)))
        self.assertCountEqual(outcomes, ["created", "already-created"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            sessions = list(pool.map(lambda now: self.auth.login("admin", ADMIN, now=now), range(2, 10)))
        self.assertEqual(len({session.token for session in sessions}), 8)
        self.assertEqual(self.auth.session_count(), 8)

    def test_logs_and_security_events_never_contain_credentials_or_tokens(self):
        self.bootstrap()
        with self.assertLogs("nexo.auth.prototype", level=logging.INFO) as captured:
            session = self.login()
            with self.assertRaises(AuthFailure):
                self.auth.login("admin", "FAKE incorrect password", now=3)
            self.auth.logout(session.token, now=4)
        output = "\n".join(captured.output) + repr(self.auth.security_events())
        for secret in (ADMIN, "FAKE incorrect password", session.token, session.csrf_token):
            self.assertNotIn(secret, output)
        self.assertNotIn("admin", "\n".join(captured.output))
        database = self.path.read_bytes()
        for secret in (ADMIN.encode(), session.token.encode(), session.csrf_token.encode()):
            self.assertNotIn(secret, database)

    def test_human_and_service_principals_are_distinct_and_scoped(self):
        self.bootstrap()
        user = self.auth.authenticate(self.login().token, "chat:read", now=3)
        with self.assertRaisesRegex(AuthFailure, "principal_type_mismatch"):
            authorize(user, "provider:invoke", expected_kind="service")
        service = service_principal("agent-runtime", frozenset({"tool:invoke"}), "run-1")
        authorize(service, "tool:invoke", expected_kind="service")
        with self.assertRaisesRegex(AuthFailure, "service_scope_denied"):
            service_principal("agent-runtime", frozenset({"system:admin"}), "run-1")
        with self.assertRaisesRegex(AuthFailure, "permission_denied"):
            authorize(service, "system:admin", expected_kind="service")

    def test_production_database_filename_is_refused(self):
        with self.assertRaises(ValueError):
            AdminAuthStore(self.path.parent / "nexo.sqlite3")
        with self.assertRaises(ValueError):
            AdminAuthStore(":memory:")

    def test_invalid_rate_window_is_rejected(self):
        with self.assertRaises(ValueError):
            AdminAuthStore(self.path.parent / "invalid-auth.sqlite3", failure_window=0)

    def test_existing_auth_database_permissions_are_restricted(self):
        self.path.chmod(0o644)
        self.auth.session_count()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_isolated_schema_preserves_unrelated_application_tables(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT)")
            connection.execute("INSERT INTO conversations VALUES('legacy','keep')")
            connection.commit()
        finally:
            connection.close()
        self.auth.initialize()
        connection = sqlite3.connect(self.path)
        try:
            row = connection.execute("SELECT id,title FROM conversations").fetchone()
            auth_tables = {item[0] for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'auth_%'")}
        finally:
            connection.close()
        self.assertEqual(row, ("legacy", "keep"))
        self.assertIn("auth_sessions", auth_tables)


class AdminAuthHttpContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth = AdminAuthStore(Path(self.temp.name) / "auth.sqlite3")
        self.auth.initialize()
        self.auth.bootstrap_local("admin", ADMIN, now=1)
        app = FastAPI()
        auth_router = create_auth_router(self.auth, ORIGIN)
        self.assertFalse(any("bootstrap" in route.path for route in auth_router.routes))
        app.include_router(auth_router)

        @app.get("/protected-read")
        def protected_read(principal=Depends(require_permission(self.auth, ORIGIN, "chat:read"))):
            return {"user": principal.subject}

        @app.post("/protected-write")
        def protected_write(principal=Depends(require_permission(self.auth, ORIGIN, "chat:write", mutating=True))):
            return {"user": principal.subject}

        @app.get("/protected-denied")
        def protected_denied(principal=Depends(require_permission(self.auth, ORIGIN, "not:granted"))):
            return {"user": principal.subject}

        self.client_context = TestClient(app, base_url=ORIGIN)
        self.client = self.client_context.__enter__()
        self.origin_headers = {"Origin": ORIGIN}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self):
        return self.client.post("/api/auth/login", headers=self.origin_headers,
                                json={"username": "admin", "password": ADMIN})

    def test_cookie_flags_and_auth_session_contract(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("token", response.json())
        cookie = response.headers["set-cookie"].lower()
        for attribute in ("secure", "httponly", "samesite=lax", "path=/"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("domain=", cookie)
        session = self.client.get("/api/auth/session")
        self.assertEqual(session.status_code, 200)
        self.assertNotIn("csrf_token", session.json())
        csrf = self.client.post("/api/auth/csrf", headers=self.origin_headers)
        self.assertTrue(csrf.json()["csrf_token"])
        self.assertEqual(self.client.get("/protected-read").status_code, 200)

    def test_401_403_csrf_and_revoked_session_contract(self):
        self.assertEqual(self.client.get("/protected-read").status_code, 401)
        logged_in = self.login()
        self.assertEqual(self.client.get("/protected-denied").status_code, 403)
        self.assertEqual(self.client.post("/protected-write", headers=self.origin_headers).status_code, 403)
        csrf_one = logged_in.json()["csrf_token"]
        csrf_two = self.client.post("/api/auth/csrf", headers=self.origin_headers).json()["csrf_token"]
        self.assertEqual(self.client.post("/protected-write", headers=self.origin_headers | {
            "X-CSRF-Token": csrf_one}).status_code, 403)
        response = self.client.post("/protected-write", headers=self.origin_headers | {"X-CSRF-Token": csrf_two})
        self.assertEqual(response.status_code, 200)
        raw = self.client.cookies.get(COOKIE_NAME)
        self.auth.revoke_session(raw, now=10)
        self.assertEqual(self.client.get("/protected-read").status_code, 401)

    def test_login_logout_and_generic_credential_errors(self):
        failed = self.client.post("/api/auth/login", headers=self.origin_headers,
                                  json={"username": "admin", "password": "FAKE bad password"})
        self.assertEqual(failed.status_code, 401)
        self.assertNotIn("FAKE", failed.text)
        self.assertNotIn("password", failed.text.lower())
        response = self.login()
        csrf = response.json()["csrf_token"]
        logout = self.client.post("/api/auth/logout", headers=self.origin_headers | {"X-CSRF-Token": csrf})
        self.assertEqual(logout.status_code, 204)
        self.assertIn("max-age=0", logout.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/protected-read").status_code, 401)

    def test_wrong_origin_host_and_http_scheme_are_rejected(self):
        wrong_origin = self.client.post("/api/auth/login", headers={"Origin": "https://evil.example.test"},
                                       json={"username": "admin", "password": ADMIN})
        self.assertEqual(wrong_origin.status_code, 400)
        wrong_host = self.client.post("/api/auth/login", headers={"Origin": ORIGIN, "Host": "evil.example.test"},
                                      json={"username": "admin", "password": ADMIN})
        self.assertEqual(wrong_host.status_code, 400)
        insecure = TestClient(self.client.app, base_url="http://nexo.example.test")
        response = insecure.post("/api/auth/login", headers=self.origin_headers,
                                 json={"username": "admin", "password": ADMIN})
        insecure.close()
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
