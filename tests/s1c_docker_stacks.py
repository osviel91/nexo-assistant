"""Disposable Docker integration check for the split NEXO/Qdrant stacks.

Run explicitly with: python tests/s1c_docker_stacks.py
This is excluded from the default quality gate because it pulls and starts Qdrant.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]
QDRANT_IMAGE = "qdrant/qdrant@sha256:b7b0444c4c351c970b98e90a6f89c2ee4287c65b44e52b4cb503fa5b2aa927ad"


def run(args: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def compose(project: str, files: list[Path], *args: str, env: dict[str, str], check: bool = True):
    command = ["docker", "compose", "--project-name", project]
    command.extend(("--env-file", env["S1C_EMPTY_ENV_FILE"]))
    for file in files:
        command.extend(("--file", str(file)))
    command.extend(args)
    return run(command, env=env, check=check)


def http_json(url: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, method=method,
                      headers={"Content-Type": "application/json"} if data else {})
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def wait_for_health(url: str, *, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return http_json(url)
        except (OSError, URLError, TimeoutError, ValueError) as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"service did not become healthy at {url}: {last_error}")


def inspect_container(container_id: str) -> dict:
    return json.loads(run(["docker", "inspect", container_id]).stdout)[0]


def service_id(project: str, files: list[Path], service: str, env: dict[str, str]) -> str:
    return compose(project, files, "ps", "--quiet", service, env=env).stdout.strip()


def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    network = f"s1c-net-{suffix}"
    qdrant_volume = f"s1c-qdrant-data-{suffix}"
    nexo_volume = f"s1c-nexo-data-{suffix}"
    missing_volume = f"s1c-missing-nexo-data-{suffix}"
    qdrant_project = f"s1c-qdrant-{suffix}"
    nexo_project = f"s1c-nexo-{suffix}"
    missing_project = f"s1c-missing-{suffix}"
    nexo_image = f"nexo-chat:s1c-{suffix}"
    qdrant_port, nexo_port = free_port(), free_port()
    collection = f"s1c-{suffix}"
    created_network = created_qdrant_volume = created_nexo_volume = built_image = False

    with tempfile.TemporaryDirectory(prefix="nexo-s1c-") as temp_dir:
        temp = Path(temp_dir)
        empty_env_file = temp / "empty.env"
        empty_env_file.write_text("")
        qdrant_override = temp / "qdrant-test.yml"
        nexo_override = temp / "nexo-test.yml"
        missing_override = temp / "nexo-missing-volume.yml"
        qdrant_override.write_text(f"""services:
  qdrant:
    ports:
      - \"127.0.0.1:{qdrant_port}:6333\"
networks:
  nexo-assistant-shared:
    name: {network}
volumes:
  qdrant-data:
    name: {qdrant_volume}
""")
        nexo_override.write_text(f"""services:
  nexo-chat:
    image: ${{NEXO_IMAGE}}
    labels:
      s1c.synthetic-release: ${{S1C_RELEASE}}
networks:
  nexo-assistant-shared:
    name: {network}
volumes:
  nexo-data:
    name: {nexo_volume}
""")
        missing_override.write_text(f"""services:
  nexo-chat:
    image: ${{NEXO_IMAGE}}
networks:
  nexo-assistant-shared:
    name: {network}
volumes:
  nexo-data:
    name: {missing_volume}
""")

        env = {key: os.environ[key] for key in (
            "PATH", "HOME", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_HOST",
            "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "DOCKER_API_VERSION",
        ) if key in os.environ} | {
            "S1C_EMPTY_ENV_FILE": str(empty_env_file),
            "NEXO_IMAGE": nexo_image,
            "NEXO_HTTP_PORT": f"127.0.0.1:{nexo_port}",
            "NEXO_DATA_DIR": "/data",
            "NEXO_VECTOR_STORE": "qdrant",
            "NEXO_QDRANT_URL": "http://qdrant:6333",
            "NEXO_QDRANT_COLLECTION": collection,
            "NEXO_MODULES": "attachments",
            "S1C_RELEASE": "release-1",
        }
        qdrant_files = [ROOT / "docker-compose.qdrant.yml", qdrant_override]
        nexo_files = [ROOT / "docker-compose.portainer.yml", nexo_override]

        try:
            run(["docker", "build", "--tag", nexo_image, "."])
            built_image = True
            run(["docker", "network", "create", network])
            created_network = True
            run(["docker", "volume", "create", qdrant_volume])
            created_qdrant_volume = True
            run(["docker", "volume", "create", nexo_volume])
            created_nexo_volume = True

            deployment_services = run([
                "docker", "compose", "--env-file", str(empty_env_file),
                "--file", str(ROOT / "docker-compose.portainer.yml"),
                "config", "--services",
            ], env=env).stdout.splitlines()
            assert deployment_services == ["nexo-chat"], deployment_services
            qdrant_services = run([
                "docker", "compose", "--env-file", str(empty_env_file),
                "--file", str(ROOT / "docker-compose.qdrant.yml"),
                "config", "--services",
            ], env=env).stdout.splitlines()
            assert qdrant_services == ["qdrant"], qdrant_services
            assert not {network, qdrant_volume, nexo_volume, missing_volume} & {
                "nexo-assistant-shared", "nexo-assistant_qdrant-data", "nexo-assistant_nexo-data"
            }

            # Each manifest is launched under a different Compose project name.
            compose(qdrant_project, qdrant_files, "up", "--detach", "qdrant", env=env)
            qdrant_id_before = service_id(qdrant_project, qdrant_files, "qdrant", env)
            wait_for_health(f"http://127.0.0.1:{qdrant_port}/healthz")

            http_json(f"http://127.0.0.1:{qdrant_port}/collections/{collection}",
                      method="PUT", body={"vectors": {"size": 2, "distance": "Cosine"}})
            http_json(f"http://127.0.0.1:{qdrant_port}/collections/{collection}/points?wait=true",
                      method="PUT", body={"points": [{"id": 1, "vector": [0.1, 0.2], "payload": {"s1c": suffix}}]})

            compose(nexo_project, nexo_files, "up", "--detach", "nexo-chat", env=env)
            nexo_id_before = service_id(nexo_project, nexo_files, "nexo-chat", env)
            health = wait_for_health(f"http://127.0.0.1:{nexo_port}/api/health")
            assert health["vector_store"] == {"status": "ok", "backend": "qdrant", "detail": None}, health
            compose(nexo_project, nexo_files, "exec", "--no-TTY", "nexo-chat", "python", "-c",
                    "from urllib.request import urlopen; assert b'healthz check passed' in urlopen('http://qdrant:6333/healthz').read()", env=env)

            qdrant_before = inspect_container(qdrant_id_before)
            nexo_before = inspect_container(nexo_id_before)
            qdrant_image_before = qdrant_before["Image"]
            qdrant_ref_before = qdrant_before["Config"]["Image"]
            qdrant_mount_before = next(m for m in qdrant_before["Mounts"] if m["Destination"] == "/qdrant/storage")
            assert qdrant_ref_before == QDRANT_IMAGE
            assert qdrant_mount_before["Name"] == qdrant_volume
            assert qdrant_before["Config"]["Labels"]["com.docker.compose.project"] == qdrant_project
            assert nexo_before["Config"]["Labels"]["com.docker.compose.project"] == nexo_project
            assert run(["docker", "ps", "--no-trunc", "--quiet", "--filter", f"volume={qdrant_volume}"]).stdout.splitlines() == [qdrant_id_before]

            # A changed release label causes Compose to recreate only NEXO.
            env["S1C_RELEASE"] = "release-2"
            compose(nexo_project, nexo_files, "up", "--detach", "--no-deps", "nexo-chat", env=env)
            nexo_id_after = service_id(nexo_project, nexo_files, "nexo-chat", env)
            qdrant_id_after = service_id(qdrant_project, qdrant_files, "qdrant", env)
            qdrant_after = inspect_container(qdrant_id_after)
            assert nexo_id_after != nexo_id_before
            assert qdrant_id_after == qdrant_id_before
            assert qdrant_after["Image"] == qdrant_image_before
            assert qdrant_after["Config"]["Image"] == qdrant_ref_before
            qdrant_mount_after = next(m for m in qdrant_after["Mounts"] if m["Destination"] == "/qdrant/storage")
            assert qdrant_mount_after["Name"] == qdrant_mount_before["Name"] == qdrant_volume
            assert run(["docker", "ps", "--no-trunc", "--quiet", "--filter", f"volume={qdrant_volume}"]).stdout.splitlines() == [qdrant_id_before]
            health_after = wait_for_health(f"http://127.0.0.1:{nexo_port}/api/health")
            assert health_after["vector_store"]["status"] == "ok", health_after
            points = http_json(f"http://127.0.0.1:{qdrant_port}/collections/{collection}/points/scroll",
                               method="POST", body={"limit": 10, "with_payload": True, "with_vector": False})
            assert points["result"]["points"][0]["payload"] == {"s1c": suffix}, points

            # An absent synthetic external volume must fail rather than be created.
            missing_env = env | {"S1C_NEXO_VOLUME": missing_volume}
            missing_result = compose(missing_project,
                                     [ROOT / "docker-compose.portainer.yml", missing_override],
                                     "up", "--detach", "nexo-chat", env=missing_env, check=False)
            assert missing_result.returncode != 0
            assert missing_volume in missing_result.stderr or missing_volume in missing_result.stdout
            assert run(["docker", "volume", "inspect", missing_volume], check=False).returncode != 0
            assert service_id(missing_project,
                              [ROOT / "docker-compose.portainer.yml", missing_override],
                              "nexo-chat", missing_env) == ""

            print(json.dumps({
                "result": "passed",
                "qdrant_project": qdrant_project,
                "nexo_project": nexo_project,
                "nexo_container_recreated": nexo_id_before != nexo_id_after,
                "qdrant_container_id": qdrant_id_after,
                "qdrant_image_ref": qdrant_ref_before,
                "qdrant_image_id": qdrant_image_before,
                "qdrant_volume": qdrant_mount_after["Name"],
                "qdrant_collection_payload_survived": points["result"]["points"][0]["payload"],
                "missing_external_volume_failed_closed": True,
            }, indent=2))
        finally:
            for project, files in ((missing_project, [ROOT / "docker-compose.portainer.yml", missing_override]),
                                   (nexo_project, nexo_files), (qdrant_project, qdrant_files)):
                compose(project, files, "down", "--remove-orphans", env=env, check=False)
            if created_nexo_volume:
                run(["docker", "volume", "rm", nexo_volume], check=False)
            if created_qdrant_volume:
                run(["docker", "volume", "rm", qdrant_volume], check=False)
            if created_network:
                run(["docker", "network", "rm", network], check=False)
            if built_image:
                run(["docker", "image", "rm", nexo_image], check=False)


if __name__ == "__main__":
    main()
