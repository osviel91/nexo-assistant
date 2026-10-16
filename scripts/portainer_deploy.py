#!/usr/bin/env python3
"""Update only the NEXO_IMAGE variable in a Portainer Compose stack."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen


class DeploymentError(RuntimeError):
    pass


def _validate_url(value: str) -> None:
    parsed = urlsplit(value)
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(
            parsed.hostname or ""
        ).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise DeploymentError("URLs must use HTTPS (HTTP is allowed only for loopback tests)")
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise DeploymentError("Invalid service URL")


def _request_json(url: str, method: str, token: str = "", body: dict | None = None):
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["X-API-Key"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    try:
        request = Request(url, data=data, headers=headers, method=method)
        with urlopen(request, timeout=10) as response:
            result = json.load(response)
    except HTTPError as error:
        raise DeploymentError(f"HTTP request failed with status {error.code}") from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise DeploymentError("HTTP request failed") from None
    if not isinstance(result, (dict, list)):
        raise DeploymentError("Unexpected API response")
    return result


def build_update_payload(stack: dict, compose: str, image: str) -> dict:
    if stack.get("Name") != "nexo-chat" or stack.get("Type") != 2:
        raise DeploymentError("Target is not the NEXO Docker Compose stack")
    if stack.get("GitConfig") or stack.get("WorkflowID"):
        raise DeploymentError("Target stack is Git-managed")
    if stack.get("Status") == 3:
        raise DeploymentError("Stack deployment is already in progress")
    if not re.fullmatch(r"ghcr\.io/[^@\s]+@sha256:[0-9a-f]{64}", image):
        raise DeploymentError("NEXO_IMAGE must be an immutable GHCR digest")
    if compose.count("${NEXO_IMAGE") != 1 or "  nexo-chat:" not in compose or "\n  qdrant:" in compose:
        raise DeploymentError("Stack Compose does not match the expected NEXO-only shape")

    env = stack.get("Env")
    if not isinstance(env, list):
        raise DeploymentError("Stack environment variables are unavailable")
    new_env = []
    image_count = 0
    for pair in env:
        if (
            not isinstance(pair, dict)
            or not isinstance(pair.get("name"), str)
            or not isinstance(pair.get("value"), str)
        ):
            raise DeploymentError("Stack environment variables are malformed")
        copied = {"name": pair["name"], "value": pair["value"]}
        if copied["name"] == "NEXO_IMAGE":
            copied["value"] = image
            image_count += 1
        new_env.append(copied)
    if image_count != 1:
        raise DeploymentError("Expected exactly one NEXO_IMAGE stack variable")

    return {
        "StackFileContent": compose,
        "Env": new_env,
        "Prune": False,
        "RepullImageAndRedeploy": True,
    }


def _health_is_ok(url: str) -> bool:
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=10) as response:
            health = json.load(response)
        return (
            isinstance(health, dict)
            and health.get("status") == "ok"
            and isinstance(health.get("vector_store"), dict)
            and health["vector_store"].get("status") == "ok"
        )
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False


def _container_uses_digest(api, endpoint_id: int, image: str) -> bool:
    labels = [
        "com.docker.compose.project=nexo-chat",
        "com.docker.compose.service=nexo-chat",
    ]
    filters = urlencode({"all": "1", "filters": json.dumps({"label": labels})})
    containers = api(f"api/endpoints/{endpoint_id}/docker/containers/json?{filters}")
    if not isinstance(containers, list):
        raise DeploymentError("Unexpected Docker container response")
    running = [
        item for item in containers
        if isinstance(item, dict) and item.get("State") == "running"
    ]
    if len(running) != 1:
        return False

    container_id = running[0].get("Id", "")
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise DeploymentError("Docker returned an invalid container identifier")
    container = api(f"api/endpoints/{endpoint_id}/docker/containers/{container_id}/json")
    if not isinstance(container, dict):
        raise DeploymentError("Unexpected Docker container inspection response")
    config = container.get("Config")
    if not isinstance(config, dict):
        raise DeploymentError("Docker returned an invalid container configuration")
    if config.get("Image") != image:
        return False

    image_id = container.get("Image", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise DeploymentError("Docker returned an invalid image identifier")
    inspected_image = api(f"api/endpoints/{endpoint_id}/docker/images/{image_id}/json")
    if not isinstance(inspected_image, dict) or not isinstance(inspected_image.get("RepoDigests"), list):
        raise DeploymentError("Unexpected Docker image inspection response")
    return image in inspected_image["RepoDigests"]


def deploy(
    base_url: str,
    token: str,
    stack_id: int,
    endpoint_id: int,
    image: str,
    health_url: str,
    *,
    timeout: int = 300,
    interval: float = 3,
) -> None:
    _validate_url(base_url)
    _validate_url(health_url)
    if not token or stack_id <= 0 or endpoint_id <= 0 or timeout <= 0 or interval < 0:
        raise DeploymentError("Invalid deployment configuration")
    base = base_url.rstrip("/") + "/"

    def api(path: str, method: str = "GET", body: dict | None = None):
        return _request_json(urljoin(base, path), method, token, body)

    stack_path = f"api/stacks/{stack_id}"
    stack = api(stack_path)
    if not isinstance(stack, dict):
        raise DeploymentError("Unexpected Portainer stack response")
    if stack.get("Id") != stack_id or stack.get("EndpointId") != endpoint_id:
        raise DeploymentError("Stack or endpoint identity did not match configuration")
    compose_response = api(f"{stack_path}/file")
    if not isinstance(compose_response, dict):
        raise DeploymentError("Unexpected Portainer Compose response")
    compose = compose_response.get("StackFileContent")
    if not isinstance(compose, str):
        raise DeploymentError("Portainer did not return the stack Compose file")
    payload = build_update_payload(stack, compose, image)
    api(f"{stack_path}?{urlencode({'endpointId': endpoint_id})}", "PUT", payload)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status_response = api(stack_path)
        if not isinstance(status_response, dict):
            raise DeploymentError("Unexpected Portainer deployment status response")
        status = status_response.get("Status")
        if status == 4:
            raise DeploymentError("Portainer reported stack deployment failure")
        if status == 2:
            raise DeploymentError("Portainer stack is inactive")
        if status == 1 and _container_uses_digest(api, endpoint_id, image) and _health_is_ok(health_url):
            return
        if status not in (1, 3):
            raise DeploymentError("Portainer returned an unknown deployment status")
        time.sleep(interval)
    raise DeploymentError("Timed out waiting for Portainer deployment and NEXO health")


def main() -> int:
    try:
        deploy(
            os.environ["PORTAINER_URL"],
            os.environ["PORTAINER_API_KEY"],
            int(os.environ["PORTAINER_STACK_ID"]),
            int(os.environ["PORTAINER_ENDPOINT_ID"]),
            os.environ["NEXO_IMAGE"],
            os.environ["NEXO_HEALTHCHECK_URL"],
            timeout=int(os.environ.get("PORTAINER_DEPLOY_TIMEOUT", "300")),
        )
    except (KeyError, ValueError):
        print("Missing or invalid deployment configuration", file=sys.stderr)
        return 2
    except DeploymentError as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        return 1
    print("Portainer deployment completed; NEXO health check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
