import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Depends, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from prototypes.admin_auth import AuthFailure
from prototypes.admin_persistence import AdministrativePersistence
from prototypes.http_authorization_guard import COOKIE_NAME, HttpAuthorizationGuard
from prototypes.http_policy_inventory import AccessPolicy, declare_policy, inspect_routes
from prototypes.security_contracts import (
    CATALOG,
    EFFECT_COUNTERS,
    EVIDENCE_LEVELS,
    INTEGRATION_REQUIREMENTS,
    OPERATIONAL_REQUIREMENTS,
    RUNTIME_REQUIREMENTS,
    MIGRATION_REQUIREMENTS,
    MIGRATION_EVIDENCE,
    OWNERSHIP_DOMAINS,
    OWNERSHIP_ACCEPTANCE,
    SSE_CHECKPOINTS,
    SyntheticEffectHarness,
    activation_blockers,
    contracts_for_modules,
    coverage_errors,
    integration_blockers,
    validate_catalog,
)


ORIGIN = "https://nexo.test"


class SecurityContractTests(unittest.TestCase):
    def test_catalog_is_versioned_explicit_and_covers_75_http_bindings(self):
        validate_catalog()
        http = [item for item in CATALOG if item.registration != "mount"]
        self.assertEqual(len(http), 75)
        self.assertEqual(len({item.id for item in CATALOG}), len(CATALOG))
        self.assertEqual(len({(item.method, item.path) for item in CATALOG}), len(CATALOG))
        self.assertEqual(sum(item.registration == "framework" for item in http), 8)
        self.assertEqual(sum(item.registration == "core" for item in http), 65)
        self.assertEqual(sum(item.registration == "module" for item in http), 2)

    def test_catalog_matches_current_audit_bindings_by_method_and_path(self):
        expected = {(item.method, item.path) for item in CATALOG}
        self.assertEqual(coverage_errors(expected, {"attachments", "decision-runtime"}), ())
        self.assertTrue(coverage_errors(expected - {("POST", "/api/chat")}, {"attachments", "decision-runtime"}))
        self.assertTrue(coverage_errors(expected | {("PUT", "/api/chat")}, {"attachments", "decision-runtime"}))

    def test_optional_module_combinations_have_exact_catalog_coverage(self):
        expected_counts = {
            frozenset(): 73,
            frozenset({"attachments"}): 74,
            frozenset({"decision-runtime"}): 74,
            frozenset({"attachments", "decision-runtime"}): 75,
        }
        for modules, count in expected_counts.items():
            with self.subTest(modules=modules):
                active = contracts_for_modules(modules)
                bindings = {(item.method, item.path) for item in active}
                self.assertEqual(len(bindings) - 1, count)
                self.assertEqual(coverage_errors(bindings, modules), ())
                app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
                with tempfile.TemporaryDirectory() as assets:
                    for contract in active:
                        if contract.registration == "mount":
                            app.mount(contract.path, StaticFiles(directory=assets), name="assets")
                            target = app.routes[-1]
                            method = "MOUNT"
                        else:
                            target = lambda: {"ok": True}
                            app.add_api_route(contract.path, target, methods=[contract.method])
                            method = contract.method
                        policy = (AccessPolicy.user(contract.permission) if contract.policy == "user" else
                                  AccessPolicy.admin(contract.permission) if contract.policy == "admin" else
                                  AccessPolicy.public())
                        declare_policy(target, method, policy)
                    snapshot = inspect_routes(app, strict=True)
                inventory = {(method, entry["path"]) for entry in snapshot.entries
                             for method in entry["methods"]}
                self.assertEqual(coverage_errors(inventory, modules), ())

    def test_all_optional_module_combinations_match_real_main_registration(self):
        combinations = (
            set(), {"attachments"}, {"decision-runtime"}, {"attachments", "decision-runtime"},
        )
        script = (
            "import json; from app.main import app; "
            "from prototypes.http_policy_inventory import inspect_routes; "
            "snapshot=inspect_routes(app); "
            "print(json.dumps([(method,entry['path']) for entry in snapshot.entries "
            "for method in entry['methods']]))"
        )
        for modules in combinations:
            with self.subTest(modules=modules), tempfile.TemporaryDirectory() as data_dir:
                env = os.environ.copy()
                env["NEXO_DATA_DIR"] = data_dir
                env["NEXO_MODULES"] = ",".join(sorted(modules))
                result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True,
                                        text=True, env=env)
                bindings = set(map(tuple, json.loads(result.stdout)))
                self.assertEqual(coverage_errors(bindings, modules), ())
                self.assertEqual(len(bindings) - 1, 73 + len(modules))

    def test_module_route_without_contract_and_route_method_drift_fail(self):
        modules = {"attachments", "decision-runtime"}
        known = {(item.method, item.path) for item in contracts_for_modules(modules)}
        self.assertIn("undeclared POST /api/optional/unclassified",
                      coverage_errors(known | {("POST", "/api/optional/unclassified")}, modules))
        self.assertIn("missing POST /api/files",
                      coverage_errors(known - {("POST", "/api/files")}, modules))
        self.assertIn("undeclared PUT /api/files",
                      coverage_errors((known - {("POST", "/api/files")}) | {("PUT", "/api/files")}, modules))

    def test_decisions_is_always_admin_diagnostics_run(self):
        item = next(item for item in CATALOG if item.path == "/api/decisions")
        self.assertEqual((item.method, item.policy, item.permission),
                         ("POST", "admin", "diagnostics:run"))

    def test_unknown_api_fallback_is_not_a_public_wildcard(self):
        fallback = next(item for item in CATALOG if item.path == "/{path:path}")
        self.assertEqual(fallback.policy, "shell-ui-allowlist")
        self.assertIn("unknown-api-is-json-404", fallback.requirements)
        self.assertNotEqual(fallback.policy, "public")

    def test_required_ownership_and_sse_boundaries_are_explicit(self):
        self.assertEqual(len(OWNERSHIP_DOMAINS), 10)
        self.assertEqual(set(OWNERSHIP_ACCEPTANCE), set(OWNERSHIP_DOMAINS))
        self.assertTrue(all("owner-predicate-atomic-with-write" in rules
                            for rules in OWNERSHIP_ACCEPTANCE.values()))
        self.assertIn("one-shot-cas", OWNERSHIP_ACCEPTANCE["approvals"])
        self.assertIn("one-shot-cas", OWNERSHIP_ACCEPTANCE["interactions"])
        self.assertTrue({"authorize_before_http_response_start", "reauthorize_before_each_protected_effect",
                         "cooperative_revocation"}.issubset(set(SSE_CHECKPOINTS)))
        self.assertIn("domain_sqlite_open", EFFECT_COUNTERS)
        self.assertIn("stream_start", EFFECT_COUNTERS)

    def test_integration_and_activation_gates_are_distinct_and_closed_by_default(self):
        self.assertEqual(set(MIGRATION_EVIDENCE), set(MIGRATION_REQUIREMENTS))
        self.assertEqual(MIGRATION_EVIDENCE["storage_failure_has_no_anonymous_fallback"],
                         "prototype-verified")
        self.assertEqual(MIGRATION_EVIDENCE["bootstrap_requires_verified_schema_and_backup"],
                         "prototype-verified")
        self.assertEqual({name for name, level in MIGRATION_EVIDENCE.items()
                          if level == "runtime-verified"}, {
            "incomplete_migration_fails_closed", "ledger_commits_with_schema",
            "schema_validated_beyond_version", "unknown_future_version_rejected",
            "incompatible_restore_fails_closed",
        })
        self.assertEqual(EVIDENCE_LEVELS, ("pending", "prototype-verified", "runtime-verified", "operational-verified"))
        prototype = {name: "prototype-verified" for name in
                     INTEGRATION_REQUIREMENTS + RUNTIME_REQUIREMENTS + MIGRATION_REQUIREMENTS + OPERATIONAL_REQUIREMENTS}
        self.assertEqual(integration_blockers(prototype), INTEGRATION_REQUIREMENTS)
        self.assertTrue(activation_blockers(prototype))
        self.assertTrue(integration_blockers({"integration_acceptance_tests_pass": True}))
        runtime = {name: "runtime-verified" for name in INTEGRATION_REQUIREMENTS + RUNTIME_REQUIREMENTS}
        self.assertEqual(integration_blockers(runtime), ())
        self.assertEqual(activation_blockers(runtime), MIGRATION_REQUIREMENTS + OPERATIONAL_REQUIREMENTS)
        ready = runtime | {name: "runtime-verified" for name in MIGRATION_REQUIREMENTS}
        ready.update({name: "operational-verified" for name in OPERATIONAL_REQUIREMENTS})
        self.assertEqual(activation_blockers(ready), ())
        self.assertEqual(len(activation_blockers({name: True for name in ready})), len(ready))


class SyntheticGuardHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth = AdministrativePersistence(Path(self.temp.name) / "synthetic-auth.sqlite3")
        self.auth.initialize()
        self.auth.bootstrap_admin("synthetic-user", "FAKE synthetic passphrase", now=1)
        self.session = self.auth.login("synthetic-user", "FAKE synthetic passphrase")
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.harness = SyntheticEffectHarness()
        self.effects = self.harness.counts
        self.counters = {name: 0 for name in ("handler", "dependency", "producer")}

    def tearDown(self):
        self.temp.cleanup()

    def guard(self, **options):
        options.setdefault("ownership_resolver", lambda *_: True)
        guard = HttpAuthorizationGuard(self.app, self.auth, allowed_origin=ORIGIN, **options)
        guard.validate()
        return guard

    def test_initial_denial_stops_body_dependencies_storage_and_all_effects(self):
        def dependency():
            self.counters["dependency"] += 1

        @self.app.post("/protected", dependencies=[Depends(dependency)])
        async def protected(request: Request):
            self.counters["handler"] += 1
            self.harness.body_read()
            self.harness.multipart_parse()
            self.harness.provider_call()
            self.harness.tool_call()
            self.harness.mcp_call()
            for name in ("domain_sqlite_open", "write", "run_create", "hook", "async_task", "stream_start"):
                self.effects[name] += 1
            await request.form()
            return {"ok": True}

        declare_policy(protected, "POST", AccessPolicy.user("chat:write"))
        guard = self.guard()

        class ReceiveCounter:
            async def __call__(_self, scope, receive, send):
                async def counted_receive():
                    self.effects["asgi_receive"] += 1
                    return await receive()

                await guard(scope, counted_receive, send)

        client = TestClient(ReceiveCounter(), base_url=ORIGIN)
        response = client.post("/protected", data={"file": "synthetic"}, headers={"origin": ORIGIN})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.counters, {"handler": 0, "dependency": 0, "producer": 0})
        self.assertEqual(set(self.effects.values()), {0})
        self.harness.assert_no_effects()

    def test_revoked_session_and_invalid_csrf_have_no_protected_effects(self):
        @self.app.post("/write")
        def write():
            self.effects["write"] += 1
            return {"ok": True}

        declare_policy(write, "POST", AccessPolicy.user("chat:write"))
        guard = self.guard()
        client = TestClient(guard, base_url=ORIGIN)
        client.cookies.set(COOKIE_NAME, self.session.token)
        headers = {"origin": ORIGIN, "x-csrf-token": "incorrect"}
        self.assertEqual(client.post("/write", headers=headers).status_code, 403)
        self.auth.revoke_session(self.session.token)
        headers["x-csrf-token"] = self.session.csrf_token
        self.assertEqual(client.post("/write", headers=headers).status_code, 401)
        self.assertEqual(self.effects["write"], 0)

    def test_storage_failure_fails_closed_before_handler(self):
        @self.app.get("/private")
        def private():
            self.counters["handler"] += 1
            return {"ok": True}

        declare_policy(private, "GET", AccessPolicy.user("chat:read"))

        class BrokenStore:
            def authenticate(self, *_args, **_kwargs):
                raise OSError("synthetic storage unavailable")

        guard = HttpAuthorizationGuard(self.app, BrokenStore(), allowed_origin=ORIGIN,
                                       ownership_resolver=lambda *_: True)
        guard.validate()
        client = TestClient(guard, base_url=ORIGIN)
        client.cookies.set(COOKIE_NAME, self.session.token)
        self.assertEqual(client.get("/private").status_code, 503)
        self.assertEqual(self.counters["handler"], 0)

    def test_unknown_api_is_404_not_spa_html_and_static_mount_is_explicit(self):
        with tempfile.TemporaryDirectory() as assets:
            (Path(assets) / "synthetic.txt").write_text("synthetic")
            self.app.mount("/assets", StaticFiles(directory=assets), name="assets")
            declare_policy(self.app.routes[-1], "MOUNT", AccessPolicy.public())

            @self.app.get("/{path:path}")
            def fallback(path: str):
                if path.startswith("api/"):
                    from fastapi import HTTPException
                    raise HTTPException(404)
                return "<html>shell</html>"

            declare_policy(fallback, "GET", AccessPolicy.public())
            guard = self.guard()
            client = TestClient(guard, base_url=ORIGIN)
            self.assertEqual(client.get("/assets/synthetic.txt").text, "synthetic")
            response = client.get("/api/not-known")
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("html", response.headers.get("content-type", ""))

    def test_late_registration_invalidates_snapshot_before_endpoint(self):
        @self.app.get("/before")
        def before():
            return {"ok": True}

        declare_policy(before, "GET", AccessPolicy.public())
        guard = self.guard()

        @self.app.get("/late")
        def late():
            self.counters["handler"] += 1
            return {"ok": True}

        declare_policy(late, "GET", AccessPolicy.public())
        self.assertEqual(TestClient(guard, base_url=ORIGIN).get("/late").status_code, 503)
        self.assertEqual(self.counters["handler"], 0)

    def test_stream_revocation_is_cooperative_between_effects(self):
        guard_ref = {}

        @self.app.get("/stream")
        async def stream(request: Request):
            async def events():
                guard_ref["guard"].authorize_effect(request, AccessPolicy.user("chat:read"))
                self.effects["tool"] += 1
                yield "data: first\n\n"
                self.auth.revoke_session(self.session.token)
                try:
                    guard_ref["guard"].authorize_effect(request, AccessPolicy.user("chat:read"))
                except AuthFailure:
                    return
                self.effects["provider_egress"] += 1
                yield "data: second\n\n"

            return StreamingResponse(events(), media_type="text/event-stream")

        declare_policy(stream, "GET", AccessPolicy.user("chat:read"))
        guard_ref["guard"] = self.guard()
        client = TestClient(guard_ref["guard"], base_url=ORIGIN)
        client.cookies.set(COOKIE_NAME, self.session.token)
        response = client.get("/stream")
        self.assertEqual(response.text, "data: first\n\n")
        self.assertEqual(self.effects["tool"], 1)
        self.assertEqual(self.effects["provider_egress"], 0)


if __name__ == "__main__":
    unittest.main()
