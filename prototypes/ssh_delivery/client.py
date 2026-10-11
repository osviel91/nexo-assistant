"""Minimal OpenSSH stdin client pinned to an exact IP and known-hosts file."""

import ipaddress
import subprocess


def send(host: str, port: int, identity: str, known_hosts: str, payload: bytes) -> subprocess.CompletedProcess:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("destination must be a pinned IP address") from None
    if not 1 <= port <= 65535:
        raise ValueError("invalid SSH port")
    command = [
        "ssh", "-F", "/dev/null", "-p", str(port), "-i", identity,
        "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes",
        "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=yes", "-o", "CheckHostIP=no",
        "-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
        "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
        "-o", "RequestTTY=no", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR",
        f"nexo-import@{host}", "ignored-client-command",
    ]
    return subprocess.run(command, input=payload, capture_output=True, timeout=15, check=False)
