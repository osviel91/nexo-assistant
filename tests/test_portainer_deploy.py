import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest
from urllib.parse import parse_qs, urlparse

from scripts.portainer_deploy import DeploymentError, build_update_payload, deploy


IMAGE = "ghcr.io/example/nexo-chat@sha256:" + "a" * 64
COMPOSE = '''services:
  nexo-chat:
    image: "${NEXO_IMAGE:?Set NEXO_IMAGE to an immutable GHCR digest reference}"
'''


class PortainerDeployTests(unittest.TestCase):
    def test_update_changes_only_nexo_image_and_preserves_other_env(self):
        stack = {
            "Name": "nexo-chat",
            "Type": 2,
            "Status": 1,
            "GitConfig": None,
            "Env": [
                {"name": "NEXO_IMAGE", "value": "ghcr.io/example/nexo-chat:old"},
                {"name": "NEXO_API_KEY", "value": "fixture-secret"},
            ],
        }

        payload = build_update_payload(stack, COMPOSE, IMAGE)

        self.assertEqual(payload["StackFileContent"], COMPOSE)
        self.assertEqual(payload["Env"], [
            {"name": "NEXO_IMAGE", "value": IMAGE},
            {"name": "NEXO_API_KEY", "value": "fixture-secret"},
        ])
        self.assertFalse(payload["Prune"])
        self.assertTrue(payload["RepullImageAndRedeploy"])

    def test_update_fails_closed_for_wrong_stack_or_missing_env(self):
        stack = {"Name": "qdrant", "Type": 2, "Status": 1, "Env": []}
        with self.assertRaises(DeploymentError):
            build_update_payload(stack, COMPOSE, IMAGE)

        stack.update(Name="nexo-chat", Env=None)
        with self.assertRaises(DeploymentError):
            build_update_payload(stack, COMPOSE, IMAGE)

    def test_update_rejects_non_digest_image(self):
        stack = {
            "Name": "nexo-chat", "Type": 2, "Status": 1, "GitConfig": None,
            "Env": [{"name": "NEXO_IMAGE", "value": "ghcr.io/example/nexo-chat:latest"}],
        }
        with self.assertRaisesRegex(DeploymentError, "immutable GHCR digest"):
            build_update_payload(stack, COMPOSE, "ghcr.io/example/nexo-chat:latest")

    def _run_with_mock_portainer(self, state, *, timeout=2):
        test_case = self
        container_id = "b" * 64
        image_id = "sha256:" + "c" * 64
        state.setdefault("health_reads", 0)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_json(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path)
                if path.path == "/api/stacks/30":
                    test_case.assertEqual(self.headers.get("X-API-Key"), "fixture-token")
                    if state.get("fail_stack_get"):
                        self.send_error(500, "fixture secret must not escape")
                        return
                    state["status_reads"] += 1
                    if "status_sequence" in state:
                        status = state["status_sequence"][min(state["status_reads"] - 1, len(state["status_sequence"]) - 1)]
                    else:
                        status = 1 if state["status_reads"] == 1 else 3 if state["status_reads"] == 2 else 1
                    self.send_json({
                        "Id": 30, "EndpointId": state.get("endpoint_id", 1), "Name": "nexo-chat", "Type": 2,
                        "Status": status, "GitConfig": None,
                        "Env": [
                            {"name": "NEXO_IMAGE", "value": "ghcr.io/example/nexo-chat:old"},
                            {"name": "NEXO_API_KEY", "value": "fixture-secret"},
                        ],
                    })
                elif path.path == "/api/stacks/30/file":
                    test_case.assertEqual(self.headers.get("X-API-Key"), "fixture-token")
                    self.send_json({"StackFileContent": COMPOSE})
                elif path.path == "/api/endpoints/1/docker/containers/json":
                    test_case.assertEqual(self.headers.get("X-API-Key"), "fixture-token")
                    query = parse_qs(path.query)
                    test_case.assertEqual(query["all"], ["1"])
                    filters = json.loads(query["filters"][0])
                    test_case.assertEqual(filters["label"], [
                        "com.docker.compose.project=nexo-chat",
                        "com.docker.compose.service=nexo-chat",
                    ])
                    self.send_json([{"Id": container_id, "State": "running"}])
                elif path.path == f"/api/endpoints/1/docker/containers/{container_id}/json":
                    test_case.assertEqual(self.headers.get("X-API-Key"), "fixture-token")
                    self.send_json({"Image": image_id, "Config": {"Image": IMAGE}})
                elif path.path == f"/api/endpoints/1/docker/images/{image_id}/json":
                    test_case.assertEqual(self.headers.get("X-API-Key"), "fixture-token")
                    self.send_json({"RepoDigests": state["repo_digests"]})
                elif path.path == "/api/health":
                    test_case.assertIsNone(self.headers.get("X-API-Key"))
                    state["health_reads"] += 1
                    self.send_json(state["health"])
                else:
                    self.send_error(404)

            def do_PUT(self):
                test_case.assertEqual(urlparse(self.path).query, "endpointId=1")
                test_case.assertEqual(self.headers.get("X-API-Key"), "fixture-token")
                state["put"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_json({"Status": 3})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            deploy(base, "fixture-token", 30, 1, IMAGE, base + "/api/health", timeout=timeout, interval=0)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(state["put"]["StackFileContent"], COMPOSE)
        self.assertEqual(state["put"]["Env"][1]["value"], "fixture-secret")
        self.assertEqual(state["put"]["Env"][0]["value"], IMAGE)
        self.assertGreaterEqual(state["status_reads"], 3)

    def test_local_mock_confirms_portainer_and_runtime_digest_before_health(self):
        state = {
            "put": None,
            "status_reads": 0,
            "repo_digests": [IMAGE],
            "health": {"status": "ok", "vector_store": {"status": "ok"}},
        }
        self._run_with_mock_portainer(state)
        self.assertEqual(state["put"]["StackFileContent"], COMPOSE)

    def test_deployment_fails_when_running_container_has_another_digest(self):
        state = {
            "put": None,
            "status_reads": 0,
            "repo_digests": ["ghcr.io/example/nexo-chat@sha256:" + "d" * 64],
            "health": {"status": "ok", "vector_store": {"status": "ok"}},
        }
        with self.assertRaisesRegex(DeploymentError, "Timed out"):
            self._run_with_mock_portainer(state, timeout=0.02)
        self.assertEqual(state["health_reads"], 0)

    def test_deployment_fails_when_health_is_not_ok(self):
        state = {
            "put": None,
            "status_reads": 0,
            "repo_digests": [IMAGE],
            "health": {"status": "ok", "vector_store": {"status": "unavailable"}},
        }
        with self.assertRaisesRegex(DeploymentError, "Timed out"):
            self._run_with_mock_portainer(state, timeout=0.02)
        self.assertGreater(state["health_reads"], 0)

    def test_portainer_deployment_error_status_fails(self):
        state = {
            "put": None,
            "status_reads": 0,
            "status_sequence": [1, 4],
            "repo_digests": [IMAGE],
            "health": {"status": "ok", "vector_store": {"status": "ok"}},
        }
        with self.assertRaisesRegex(DeploymentError, "deployment failure"):
            self._run_with_mock_portainer(state)

    def test_deployer_refuses_a_different_portainer_endpoint_before_put(self):
        state = {
            "put": None,
            "status_reads": 0,
            "endpoint_id": 29,
            "repo_digests": [IMAGE],
            "health": {"status": "ok", "vector_store": {"status": "ok"}},
        }
        with self.assertRaisesRegex(DeploymentError, "identity did not match"):
            self._run_with_mock_portainer(state)
        self.assertIsNone(state["put"])

    def test_portainer_http_error_does_not_expose_response_body(self):
        state = {
            "put": None,
            "status_reads": 0,
            "repo_digests": [IMAGE],
            "health": {"status": "ok", "vector_store": {"status": "ok"}},
            "fail_stack_get": True,
        }
        with self.assertRaises(DeploymentError) as error:
            self._run_with_mock_portainer(state)
        self.assertNotIn("fixture secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
