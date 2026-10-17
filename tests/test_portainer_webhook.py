from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from threading import Thread
import unittest
from unittest.mock import patch

from scripts.portainer_webhook import WebhookError, main, post_webhook


WEBHOOK_PATH = "/api/stacks/webhooks/12345678-1234-4234-8234-123456789abc"


class PortainerWebhookTests(unittest.TestCase):
    def _serve(self, statuses):
        state = {"statuses": list(statuses), "requests": []}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                state["requests"].append((self.command, self.path))
                status = state["statuses"].pop(0) if state["statuses"] else 204
                body = b"fixture response must not appear in logs"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}{WEBHOOK_PATH}"
        return state, server, thread, url

    def test_posts_webhook_and_never_prints_its_url(self):
        state, server, thread, url = self._serve([204])
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with (
                patch.dict(os.environ, {"NEXO_PORTAINER_WEBHOOK_URL": url}),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = main()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(result, 0)
        self.assertEqual(state["requests"], [("POST", WEBHOOK_PATH)])
        self.assertIn("HTTP 204", stdout.getvalue())
        self.assertNotIn(url, stdout.getvalue() + stderr.getvalue())

    def test_retries_busy_stack_and_succeeds(self):
        state, server, thread, url = self._serve([409, 204])
        try:
            status = post_webhook(url, timeout=2, retry_interval=0)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(status, 204)
        self.assertEqual(state["requests"], [("POST", WEBHOOK_PATH), ("POST", WEBHOOK_PATH)])

    def test_failed_response_does_not_expose_url_or_body(self):
        state, server, thread, url = self._serve([500])
        try:
            with self.assertRaises(WebhookError) as error:
                post_webhook(url, timeout=1, retry_interval=0)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertNotIn(url, str(error.exception))
        self.assertNotIn("fixture response", str(error.exception))

    def test_rejects_non_webhook_urls_and_redirects(self):
        with self.assertRaises(WebhookError):
            post_webhook("https://portainer.example/not-a-stack-webhook")

        state, server, thread, url = self._serve([302])
        try:
            with self.assertRaisesRegex(WebhookError, "HTTP 302"):
                post_webhook(url, timeout=1, retry_interval=0)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(len(state["requests"]), 1)


if __name__ == "__main__":
    unittest.main()
