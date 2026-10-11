import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from prototypes.admin_auth import AuthFailure, Principal
from prototypes.admin_persistence import AdministrativePersistence
from prototypes.http_authorization_guard import COOKIE_NAME, HttpAuthorizationGuard, ServiceCredential
from prototypes.http_policy_inventory import AccessPolicy, declare_policy


ORIGIN = "https://nexo.example.test"
PASSWORD = "FAKE stage 15B guard password"


class HttpAuthorizationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth = AdministrativePersistence(Path(self.temp.name) / "auth.sqlite3")
        self.auth.initialize()
        self.auth.bootstrap_admin("synthetic-admin", PASSWORD, now=1)
        self.session = self.auth.login("synthetic-admin", PASSWORD)
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.calls = {"handler": 0, "dependency": 0, "body": 0, "producer": 0}
        self.effects = {name: 0 for name in ("sqlite_write", "conversation_create", "run_create",
                                             "tool", "provider", "protected_file_read", "sse_start")}

    def tearDown(self):
        self.temp.cleanup()

    def guard(self, **kwargs):
        kwargs.setdefault("ownership_resolver", lambda *_: True)
        result = HttpAuthorizationGuard(self.app, self.auth, allowed_origin=ORIGIN, **kwargs)
        result.validate()
        return result

    def client(self, guard):
        client = TestClient(guard, base_url=ORIGIN, raise_server_exceptions=False)
        client.cookies.set(COOKIE_NAME, self.session.token)
        return client

    def test_route_policy_is_required_at_validation_and_unvalidated_guard_fails_closed(self):
        self.app.get("/private")(lambda: {"ok": True})
        guard = HttpAuthorizationGuard(self.app, self.auth, allowed_origin=ORIGIN)
        response = TestClient(guard, base_url=ORIGIN).get("/private")
        self.assertEqual(response.status_code, 503)
        with self.assertRaises(Exception):
            guard.validate()

    def test_denial_precedes_dependencies_body_parsing_and_handler(self):
        from fastapi import Depends

        def dependency():
            self.calls["dependency"] += 1

        @self.app.post("/write", dependencies=[Depends(dependency)])
        async def write(request: Request):
            self.calls["handler"] += 1
            for effect in self.effects:
                self.effects[effect] += 1
            await request.body()
            self.calls["body"] += 1
            return {"ok": True}

        declare_policy(write, "POST", AccessPolicy.user("chat:write"))
        guard = self.guard()
        client = TestClient(guard, base_url=ORIGIN)
        response = client.post("/write", content=b"not parsed", headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.calls, {"handler": 0, "dependency": 0, "body": 0, "producer": 0})
        self.assertEqual(set(self.effects.values()), {0})

    def test_csrf_origin_host_and_authenticated_request_matrix(self):
        @self.app.post("/write")
        def write():
            self.calls["handler"] += 1
            return {"ok": True}

        declare_policy(write, "POST", AccessPolicy.user("chat:write"))
        client = self.client(self.guard())
        self.assertEqual(client.post("/write", headers={"origin": ORIGIN}).status_code, 403)
        self.assertEqual(client.post("/write", headers={"x-csrf-token": self.session.csrf_token}).status_code, 400)
        self.assertEqual(client.post("/write", headers={"origin": "https://evil.invalid",
                                                        "x-csrf-token": self.session.csrf_token}).status_code, 400)
        self.assertEqual(client.post("/write", headers={"origin": ORIGIN, "host": "evil.invalid",
                                                        "x-csrf-token": self.session.csrf_token}).status_code, 400)
        self.assertEqual(client.post("/write", content="<html>", headers={"origin": ORIGIN,
                                "x-csrf-token": self.session.csrf_token,
                                "content-type": "text/html"}).status_code, 415)
        self.assertEqual(client.post("/write", headers={"origin": ORIGIN,
                                                        "x-csrf-token": self.session.csrf_token}).status_code, 200)
        self.assertEqual(self.calls["handler"], 1)

    def test_permissions_and_session_revocation_are_checked_before_handler(self):
        @self.app.get("/private")
        def private():
            self.calls["handler"] += 1
            return {"ok": True}

        declare_policy(private, "GET", AccessPolicy.user("not:granted"))
        client = self.client(self.guard())
        self.assertEqual(client.get("/private").status_code, 403)
        self.auth.revoke_session(self.session.token)
        self.assertEqual(client.get("/private").status_code, 401)
        self.assertEqual(self.calls["handler"], 0)

    def test_ownership_is_hidden_and_storage_errors_fail_closed(self):
        @self.app.get("/items/{item_id}")
        def item(item_id: str):
            self.calls["handler"] += 1
            return {"id": item_id}

        declare_policy(item, "GET", AccessPolicy.user("chat:read"))
        client = self.client(self.guard(ownership_resolver=lambda *_: False))
        self.assertEqual(client.get("/items/owned-by-other").status_code, 404)
        self.assertEqual(self.calls["handler"], 0)
        resolver = Mock(side_effect=RuntimeError("FAKE sqlite path and SQL"))
        client = self.client(self.guard(ownership_resolver=resolver))
        response = client.get("/items/123")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("FAKE", response.text)

    def test_missing_ownership_resolver_fails_closed(self):
        @self.app.get("/private")
        def private():
            self.calls["handler"] += 1
            return {"ok": True}

        declare_policy(private, "GET", AccessPolicy.user("chat:read"))
        guard = HttpAuthorizationGuard(self.app, self.auth, allowed_origin=ORIGIN)
        guard.validate()
        response = self.client(guard).get("/private")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.calls["handler"], 0)

    def test_service_identity_requires_injected_verifier_scope_and_audience(self):
        @self.app.post("/service")
        def service():
            self.calls["handler"] += 1
            return {"ok": True}

        declare_policy(service, "POST", AccessPolicy.service("tool:invoke", "agent-runtime"))

        def verifier(request):
            if request.headers.get("authorization") != "Bearer fake-test-credential":
                return None
            return ServiceCredential(Principal("service", "synthetic-agent", frozenset({"tool:invoke"})),
                                     "agent-runtime", frozenset({"tool:invoke"}))

        client = TestClient(self.guard(service_verifier=verifier), base_url=ORIGIN)
        self.assertEqual(client.post("/service", headers={"x-service": "agent-runtime"}).status_code, 401)
        self.assertEqual(client.post("/service", headers={"authorization": "Bearer fake-test-credential"}).status_code, 200)
        self.assertEqual(self.calls["handler"], 1)
        unverified_headers = TestClient(self.guard(), base_url=ORIGIN)
        for headers in ({"x-service": "agent-runtime"},
                        {"authorization": "Bearer fake-test-credential"}):
            with self.subTest(headers=headers):
                self.assertEqual(unverified_headers.post("/service", headers=headers).status_code, 401)

    def test_delegation_expiry_revocation_and_scope_narrowing(self):
        @self.app.post("/delegated")
        def delegated():
            self.calls["handler"] += 1
            return {"ok": True}

        policy = AccessPolicy.service("tool:invoke", "agent-runtime")
        declare_policy(delegated, "POST", policy)
        credential = {"value": ServiceCredential(
            Principal("service", "synthetic-agent", frozenset({"tool:invoke", "provider:invoke"})),
            "agent-runtime", frozenset({"tool:invoke", "provider:invoke"}),
            owner_user_id="synthetic-owner", delegation_scopes=frozenset({"tool:invoke"}),
            expires_at=time.time() + 60,
        )}

        def verifier(_request):
            return credential["value"]

        guard = self.guard(service_verifier=verifier,
                           ownership_resolver=lambda _request, delegated, _policy:
                           delegated.owner_user_id == "synthetic-owner")
        client = TestClient(guard, base_url=ORIGIN)
        self.assertEqual(client.post("/delegated").status_code, 200)
        request = Request({"type": "http", "method": "POST", "path": "/delegated", "headers": [],
                           "scheme": "https", "server": ("nexo.example.test", 443),
                           "client": ("test", 1), "query_string": b""})
        self.assertEqual(guard.authorize_service_effect(request, policy).owner_user_id, "synthetic-owner")
        credential["value"] = ServiceCredential(
            credential["value"].principal, "agent-runtime", frozenset({"tool:invoke"}),
            owner_user_id="synthetic-owner", delegation_scopes=frozenset({"tool:invoke"}),
            expires_at=time.time() - 1,
        )
        self.assertEqual(client.post("/delegated").status_code, 403)
        credential["value"] = ServiceCredential(
            credential["value"].principal, "agent-runtime", frozenset({"tool:invoke"}),
            owner_user_id="synthetic-owner", delegation_scopes=frozenset({"tool:invoke", "admin:*"}),
            expires_at=time.time() + 60,
        )
        self.assertEqual(client.post("/delegated").status_code, 403)
        credential["value"] = ServiceCredential(
            credential["value"].principal, "agent-runtime", frozenset({"tool:invoke"}),
            owner_user_id="synthetic-owner", delegation_scopes=frozenset({"tool:invoke"}),
            expires_at=time.time() + 60, revoked=True,
        )
        self.assertEqual(client.post("/delegated").status_code, 403)
        with self.assertRaises(AuthFailure):
            guard.authorize_service_effect(request, policy)
        self.assertEqual(self.calls["handler"], 1)

    def test_admin_policy_requires_explicit_admin_identity_verification(self):
        @self.app.get("/admin")
        def admin():
            self.calls["handler"] += 1
            return {"ok": True}

        declare_policy(admin, "GET", AccessPolicy.admin("config:read"))
        denied = self.client(self.guard()).get("/admin")
        self.assertEqual(denied.status_code, 403)
        allowed = self.client(self.guard(admin_verifier=lambda _request, principal: principal.subject == "synthetic-admin")).get("/admin")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(self.calls["handler"], 1)

    def test_static_mount_is_explicit_and_unknown_api_fallback_is_404(self):
        with tempfile.TemporaryDirectory() as assets:
            (Path(assets) / "test.txt").write_text("synthetic asset")
            self.app.mount("/assets", StaticFiles(directory=assets), name="assets")
            declare_policy(self.app.routes[-1], "MOUNT", AccessPolicy.public())

            @self.app.get("/{path:path}")
            def spa(path: str):
                return "<html>synthetic shell</html>"

            declare_policy(spa, "GET", AccessPolicy.public())
            client = TestClient(self.guard(), base_url=ORIGIN)
            self.assertEqual(client.get("/assets/test.txt").text, "synthetic asset")
            response = client.get("/api/not-registered")
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("html", response.headers.get("content-type", ""))

    def test_late_route_registration_invalidates_snapshot(self):
        @self.app.get("/before")
        def before():
            return {"ok": True}

        declare_policy(before, "GET", AccessPolicy.public())
        guard = self.guard()

        @self.app.get("/late")
        def late():
            self.calls["handler"] += 1
            return {"ok": True}

        declare_policy(late, "GET", AccessPolicy.public())
        response = TestClient(guard, base_url=ORIGIN).get("/late")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.calls["handler"], 0)
        guard.validate()
        response = TestClient(guard, base_url=ORIGIN).get("/late")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls["handler"], 1)

    def test_sse_generator_is_not_started_without_auth_and_revocation_is_rechecked(self):
        guard_ref = {}
        revoke_before_start = {"enabled": False}

        @self.app.post("/events")
        async def events(request: Request):
            if revoke_before_start["enabled"]:
                self.auth.revoke_session(self.session.token)

            async def generate():
                guard_ref["guard"].authorize_effect(request, AccessPolicy.user("chat:write"))
                self.calls["producer"] += 1
                yield "data: synthetic\n\n"

            self.calls["handler"] += 1
            return StreamingResponse(generate(), media_type="text/event-stream")

        declare_policy(events, "POST", AccessPolicy.user("chat:write"))
        guard = self.guard()
        guard_ref["guard"] = guard
        anonymous = TestClient(guard, base_url=ORIGIN, raise_server_exceptions=False)
        self.assertEqual(anonymous.post("/events", headers={"origin": ORIGIN}).status_code, 401)
        self.assertEqual(self.calls["handler"], 0)

        client = self.client(guard)
        revoke_before_start["enabled"] = True
        response = client.post("/events", headers={"origin": ORIGIN, "x-csrf-token": self.session.csrf_token})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.calls["handler"], 1)
        self.assertEqual(self.calls["producer"], 0)

    def test_sse_revocation_between_protected_effects_prevents_second_effect(self):
        guard_ref = {}
        effects = {"first": 0, "second": 0, "revocation_denied": 0}
        policy = AccessPolicy.user("chat:read")

        @self.app.get("/events")
        async def events(request: Request):
            async def generate():
                guard_ref["guard"].authorize_effect(request, policy)
                effects["first"] += 1
                yield "data: first\n\n"

                self.auth.revoke_session(self.session.token)
                try:
                    guard_ref["guard"].authorize_effect(request, policy)
                except AuthFailure:
                    effects["revocation_denied"] += 1
                    return
                effects["second"] += 1
                yield "data: second\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")

        declare_policy(events, "GET", policy)
        guard = self.guard()
        guard_ref["guard"] = guard
        response = self.client(guard).get("/events")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "data: first\n\n")
        self.assertEqual(effects, {"first": 1, "second": 0, "revocation_denied": 1})

    def test_auth_storage_error_fails_closed_without_leaking_details(self):
        @self.app.get("/private")
        def private():
            self.calls["handler"] += 1
            return {"ok": True}

        declare_policy(private, "GET", AccessPolicy.user("chat:read"))
        unavailable = Mock()
        unavailable.authenticate.side_effect = RuntimeError("FAKE sqlite path and query")
        guard = HttpAuthorizationGuard(self.app, unavailable, allowed_origin=ORIGIN,
                                       ownership_resolver=lambda *_: True)
        guard.validate()
        response = self.client(guard).get("/private")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("FAKE", response.text)
        self.assertEqual(self.calls["handler"], 0)


if __name__ == "__main__":
    unittest.main()
