#!/usr/bin/env python3
"""POST to a Portainer stack webhook without exposing its bearer URL."""

from __future__ import annotations

import ipaddress
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class WebhookError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(
            parsed.hostname or ""
        ).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise WebhookError("Webhook URL must use HTTPS")
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.search(
            r"(?:^|/)api/stacks/webhooks/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$",
            parsed.path,
        )
    ):
        raise WebhookError("Invalid Portainer stack webhook URL")


def post_webhook(url: str, *, timeout: int = 300, retry_interval: float = 5) -> int:
    _validate_url(url)
    if timeout <= 0 or retry_interval < 0:
        raise WebhookError("Invalid webhook timeout")

    opener = build_opener(_NoRedirect)
    deadline = time.monotonic() + timeout
    while True:
        try:
            request = Request(url, method="POST")
            request_timeout = min(10, max(0.1, deadline - time.monotonic()))
            with opener.open(request, timeout=request_timeout) as response:
                status = response.status
        except HTTPError as error:
            status = error.code
            error.close()
        except (URLError, TimeoutError, OSError):
            raise WebhookError("Portainer webhook request failed") from None

        if 200 <= status < 300:
            return status
        # ponytail: retry only Portainer's transient in-progress response; other statuses need review.
        if status != 409 or time.monotonic() >= deadline:
            raise WebhookError(f"Portainer webhook rejected request (HTTP {status})")
        time.sleep(min(retry_interval, max(0, deadline - time.monotonic())))


def main() -> int:
    try:
        status = post_webhook(
            os.environ["NEXO_PORTAINER_WEBHOOK_URL"],
            timeout=int(os.environ.get("NEXO_PORTAINER_WEBHOOK_TIMEOUT", "300")),
        )
    except (KeyError, ValueError):
        print("Missing or invalid webhook configuration", file=sys.stderr)
        return 2
    except WebhookError as error:
        print(f"Portainer webhook failed: {error}", file=sys.stderr)
        return 1
    print(f"Portainer webhook accepted (HTTP {status})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
