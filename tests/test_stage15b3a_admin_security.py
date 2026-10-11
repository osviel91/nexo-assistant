import unittest

from prototypes.admin_security import AdminSecurity, AuthError


class AdminSecurityTests(unittest.TestCase):
    def setUp(self):
        self.auth = AdminSecurity("admin-1", "FAKE strong test password 123!", session_ttl=60,
                                  max_attempts=2, lock_seconds=30)

    def test_admin_session_csrf_expiry_and_revocation(self):
        token, csrf = self.auth.login("admin-1", "FAKE strong test password 123!", 100)
        self.assertEqual(self.auth.authorize(token, "admin:*", now=101), "admin-1")
        with self.assertRaisesRegex(AuthError, "csrf"):
            self.auth.authorize(token, "admin:*", now=101, mutating=True)
        self.assertEqual(self.auth.authorize(token, "admin:*", now=101, csrf_token=csrf, mutating=True), "admin-1")
        with self.assertRaisesRegex(AuthError, "authentication required"):
            self.auth.authorize(token, "admin:*", now=160)
        token, _ = self.auth.login("admin-1", "FAKE strong test password 123!", 200)
        self.auth.logout(token)
        with self.assertRaisesRegex(AuthError, "authentication required"):
            self.auth.authorize(token, "admin:*", now=201)

    def test_bad_credentials_lock_out_and_anonymous_is_denied(self):
        for now in (10, 11):
            with self.assertRaises(AuthError):
                self.auth.login("admin-1", "wrong", now)
        with self.assertRaises(AuthError):
            self.auth.login("admin-1", "FAKE strong test password 123!", 12)
        with self.assertRaises(AuthError):
            self.auth.authorize("", "admin:*", now=12)

    def test_default_deny_and_service_identity_is_separate(self):
        token, csrf = self.auth.login("admin-1", "FAKE strong test password 123!", 1)
        with self.assertRaisesRegex(AuthError, "permission denied"):
            self.auth.authorize(token, "providers:write", now=2)
        with self.assertRaisesRegex(AuthError, "service identity required"):
            self.auth.authorize_service("agent-runtime", "provider:use")

    def test_bootstrap_rejects_weak_initial_password(self):
        with self.assertRaises(ValueError):
            AdminSecurity("admin-1", "short")


if __name__ == "__main__":
    unittest.main()
