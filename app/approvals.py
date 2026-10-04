from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any


SECRET_KEY = re.compile(r"(?:authorization|token|secret|password|credential|cookie|api[_-]?key|bearer)", re.I)
SAFE_FIELDS = {"service", "environment", "resource", "namespace", "action", "name"}


def freeze_arguments(arguments: dict[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


def tool_fingerprint(tool) -> str:
    identity = {"name": tool.name, "source": tool.source, "module_id": tool.module_id,
                "action": tool.action or "unknown", "parameters": tool.parameters,
                "handler": f"{tool.handler.__module__}:{tool.handler.__qualname__}"}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _preview(value: Any, key: str = "", depth: int = 0) -> Any:
    if SECRET_KEY.search(key):
        return "[redacted]"
    if depth >= 5:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(name)[:100]: _preview(item, str(name), depth + 1) for name, item in list(value.items())[:40]}
    if isinstance(value, list):
        return [_preview(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, str):
        if re.search(r"(?i)(bearer\s|(?:api[_-]?key|password|token|secret)=|https?://[^\s:@]+:[^\s@]+@)", value):
            return "[redacted]"
        return value[:500] + ("…" if len(value) > 500 else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def safe_summary(tool_id: str, action: str, arguments: dict[str, Any], description: str = "") -> dict[str, Any]:
    preview = _preview(arguments)
    while len(json.dumps(preview, ensure_ascii=False)) > 6000 and isinstance(preview, dict) and preview:
        preview.pop(next(reversed(preview)))
    details = {key: value for key, value in preview.items() if key.casefold() in SAFE_FIELDS} if isinstance(preview, dict) else {}
    return {"tool_id": tool_id[:160], "action": action, "description": description[:500], "arguments": preview, "details": details}


def create(connection, *, run_id: str, conversation_id: str, message_id: str, tool_call_id: str,
           tool_id: str, action: str, arguments: dict[str, Any], continuation: dict[str, Any], fingerprint: str,
           description: str = "") -> str:
    encoded, digest = freeze_arguments(arguments)
    approval_id = str(uuid.uuid4())
    connection.execute("""INSERT INTO pending_approvals
        (id,run_id,conversation_id,message_id,tool_call_id,tool_id,action,tool_fingerprint,frozen_arguments,
         arguments_sha256,safe_summary,continuation,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (approval_id, run_id, conversation_id, message_id, tool_call_id, tool_id, action,
         fingerprint, encoded, digest, json.dumps(safe_summary(tool_id, action, arguments, description)),
         json.dumps(continuation, ensure_ascii=False, separators=(",", ":"))))
    return approval_id


def resolve(connection, approval_id: str, decision: str) -> dict[str, Any] | None:
    if decision not in {"approve", "reject"}:
        raise ValueError("invalid_decision")
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT * FROM pending_approvals WHERE id=?", (approval_id,)).fetchone()
    if row is None:
        return None
    status = "approved" if decision == "approve" else "rejected"
    cursor = connection.execute("UPDATE pending_approvals SET status=?,resolution=?,resolved_at=datetime('now') WHERE id=? AND status='pending'",
                                (status, decision, approval_id))
    if cursor.rowcount != 1:
        return {"status": row["status"], "won": False}
    resolved = dict(row)
    resolved.update(status=status, resolution=decision, won=True)
    return resolved
