import tempfile
import unittest

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from prototypes.http_policy_inventory import (
    AccessPolicy,
    PolicyConflict,
    UnclassifiedRoutes,
    assert_snapshot_current,
    declare_policy,
    inspect_routes,
)


class HttpPolicyInventoryTests(unittest.TestCase):
    def test_explicit_policy_kinds_and_per_method_policy(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/public")
        def public():
            return {"ok": True}

        declare_policy(public, "GET", AccessPolicy.public())

        @app.get("/user")
        def user_read():
            return {"ok": True}

        declare_policy(user_read, "GET", AccessPolicy.user("chat:read"))

        @app.get("/admin")
        def admin_read():
            return {"ok": True}

        declare_policy(admin_read, "GET", AccessPolicy.admin("config:read"))

        @app.get("/service")
        def service_read():
            return {"ok": True}

        declare_policy(service_read, "GET", AccessPolicy.service("rag:query", "retrieval"))

        @app.get("/same")
        def same_get():
            return {"ok": True}

        @app.post("/same")
        def same_post():
            return {"ok": True}

        declare_policy(same_get, "GET", AccessPolicy.user("chat:read"))
        declare_policy(same_post, "POST", AccessPolicy.admin("chat:write"))

        report = inspect_routes(app, strict=True)
        by_path = {entry["path"]: entry for entry in report.entries}
        self.assertEqual(by_path["/public"]["policies"]["GET"]["kind"], "public")
        self.assertEqual(by_path["/user"]["policies"]["GET"]["permission"], "chat:read")
        self.assertEqual(by_path["/admin"]["policies"]["GET"]["permission"], "config:read")
        self.assertEqual(by_path["/service"]["policies"]["GET"],
                         {"kind": "service", "scope": "rag:query", "audience": "retrieval"})
        same = [entry for entry in report.entries if entry["path"] == "/same"]
        self.assertEqual([entry["methods"] for entry in same], [["GET"], ["POST"]])

    def test_unclassified_and_strict_fail_closed(self):
        app = FastAPI()
        app.get("/private")(lambda: {"ok": True})
        snapshot = inspect_routes(app)
        self.assertIn("missing_policy", {item["code"] for item in snapshot.findings})
        with self.assertRaises(UnclassifiedRoutes):
            inspect_routes(app, strict=True)

    def test_missing_policy_findings_are_per_method_and_mount(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.api_route("/multi-method", methods=["GET", "POST"])(lambda: {"ok": True})
        with tempfile.TemporaryDirectory() as directory:
            app.mount("/assets", StaticFiles(directory=directory), name="assets")
            missing = [finding for finding in inspect_routes(app).findings
                       if finding["code"] == "missing_policy"]
        self.assertEqual({finding["method"] for finding in missing}, {"GET", "POST", "MOUNT"})
        self.assertEqual(len(missing), 3)

    def test_duplicate_policy_declaration_is_rejected(self):
        def endpoint():
            return None

        declare_policy(endpoint, "GET", AccessPolicy.public())
        with self.assertRaises(PolicyConflict):
            declare_policy(endpoint, "GET", AccessPolicy.user("chat:read"))

    def test_wildcard_and_incomplete_policy_values_are_rejected(self):
        with self.assertRaises(ValueError):
            AccessPolicy.user("chat:*")
        with self.assertRaises(ValueError):
            AccessPolicy.service("agent:run", "")
        with self.assertRaises(ValueError):
            AccessPolicy(AccessPolicy.public().kind, permission="chat:read")
        with self.assertRaises(ValueError):
            AccessPolicy("user", permission="chat:read")

    def test_extension_http_methods_are_inventoryable(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.api_route("/custom-method", methods=["PROPFIND"])
        def custom_method():
            return {"ok": True}

        declare_policy(custom_method, "PROPFIND", AccessPolicy.admin("config:read"))
        self.assertEqual(inspect_routes(app, strict=True).entries[0]["methods"], ["PROPFIND"])

    def test_framework_generated_routes_are_identified_not_public_by_default(self):
        app = FastAPI()
        snapshot = inspect_routes(app)
        docs = [entry for entry in snapshot.entries if entry["path"] == "/docs"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["source"], "framework")
        self.assertIn("missing_policy", {item["code"] for item in snapshot.findings})

    def test_static_mount_requires_explicit_policy(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        with tempfile.TemporaryDirectory() as directory:
            app.mount("/assets", StaticFiles(directory=directory), name="assets")
            mount = app.routes[-1]
            declare_policy(mount, "MOUNT", AccessPolicy.public())
            report = inspect_routes(app, strict=True)
        self.assertEqual(report.entries[0]["kind"], "mount")
        self.assertEqual(report.entries[0]["source"], "framework:static-files")
        self.assertEqual(report.entries[0]["policies"]["MOUNT"]["kind"], "public")

    def test_nested_mount_and_route_are_both_inventoried(self):
        child = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @child.get("/items/{item_id}")
        def item(item_id: str):
            return {"id": item_id}

        declare_policy(item, "GET", AccessPolicy.user("rag:read"))
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.mount("/nested", child, name="nested")
        declare_policy(app.routes[-1], "MOUNT", AccessPolicy.user("rag:read"))
        report = inspect_routes(app, strict=True)
        self.assertEqual([entry["path"] for entry in report.entries],
                         ["/nested", "/nested/items/{item_id}"])
        self.assertEqual(report.entries[1]["policies"]["GET"]["permission"], "rag:read")

    def test_optional_module_registered_route_is_inventoried(self):
        class OptionalModule:
            def register(self, app):
                @app.post("/optional/action")
                def action():
                    return {"ok": True}

                declare_policy(action, "POST", AccessPolicy.service("decision:run", "decision-runtime"))

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        OptionalModule().register(app)
        entry = inspect_routes(app, strict=True).entries[0]
        self.assertEqual(entry["path"], "/optional/action")
        self.assertEqual(entry["methods"], ["POST"])

    def test_websocket_route_is_a_distinct_inventory_kind(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.websocket("/ws")
        async def socket(websocket: WebSocket):
            await websocket.close()

        declare_policy(socket, "WEBSOCKET", AccessPolicy.user("chat:read"))
        entry = inspect_routes(app, strict=True).entries[0]
        self.assertEqual((entry["kind"], entry["methods"]), ("websocket", ["WEBSOCKET"]))

    def test_duplicate_and_overlapping_routes_report_registration_conflicts(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/items/{item_id}")
        def by_id(item_id: str):
            return {"id": item_id}

        declare_policy(by_id, "GET", AccessPolicy.user("items:read"))

        @app.get("/items/current")
        def current():
            return {"id": "current"}

        declare_policy(current, "GET", AccessPolicy.user("items:read"))
        report = inspect_routes(app)
        overlap = next(item for item in report.findings if item["code"] == "matching_overlap")
        self.assertEqual(overlap["conflicts_with"], 0)

    def test_catch_all_order_can_be_reported_as_overlap(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/{path:path}")
        def catch_all(path: str):
            return {"path": path}

        declare_policy(catch_all, "GET", AccessPolicy.public())

        @app.get("/later")
        def later():
            return {"ok": True}

        declare_policy(later, "GET", AccessPolicy.admin("config:read"))
        report = inspect_routes(app)
        overlap = next(item for item in report.findings if item["code"] == "matching_overlap")
        self.assertEqual(overlap["conflicts_with"], 0)
        with self.assertRaises(UnclassifiedRoutes):
            inspect_routes(app, strict=True)

    def test_path_parameter_does_not_overlap_its_parent_without_trailing_slash(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.patch("/tools")
        def all_tools():
            return {"ok": True}

        declare_policy(all_tools, "PATCH", AccessPolicy.admin("tools:write"))

        @app.patch("/tools/{tool_id:path}")
        def one_tool(tool_id: str):
            return {"tool": tool_id}

        declare_policy(one_tool, "PATCH", AccessPolicy.admin("tools:write"))
        report = inspect_routes(app, strict=True)
        self.assertNotIn("matching_overlap", {item["code"] for item in report.findings})

    def test_policy_method_mismatch_is_an_error(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/only-get")
        def only_get():
            return {"ok": True}

        declare_policy(only_get, "POST", AccessPolicy.user("chat:write"))
        report = inspect_routes(app)
        codes = {item["code"] for item in report.findings}
        self.assertTrue({"missing_policy", "policy_method_mismatch"}.issubset(codes))

    def test_late_registration_invalidates_frozen_snapshot(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/before")
        def before():
            return {"ok": True}

        declare_policy(before, "GET", AccessPolicy.public())
        snapshot = inspect_routes(app, strict=True)
        self.assertTrue(snapshot.valid)

        @app.get("/late")
        def late():
            return {"ok": True}

        declare_policy(late, "GET", AccessPolicy.public())
        with self.assertRaisesRegex(RuntimeError, "stale"):
            assert_snapshot_current(app, snapshot)
        self.assertTrue(snapshot.stale)
        self.assertFalse(snapshot.valid)
        self.assertTrue(inspect_routes(app, strict=True).valid)

    def test_snapshot_output_is_deterministic(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/deterministic")
        def endpoint():
            return {"ok": True}

        declare_policy(endpoint, "GET", AccessPolicy.public())
        first = inspect_routes(app)
        second = inspect_routes(app)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.to_dict(), second.to_dict())


if __name__ == "__main__":
    unittest.main()
