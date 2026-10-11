#!/usr/bin/env python3
"""Restricted SSH stdin receiver; stores only one-use transaction markers."""

import hashlib
import json
import os
import sys
import time
import uuid


MAX_PAYLOAD_BYTES = 16 * 1024
MAX_TTL_SECONDS = 300
REQUIRED_CREDENTIALS = {"access_token", "refresh_token", "client_id"}
ALLOWED_CREDENTIALS = REQUIRED_CREDENTIALS | {
    "id_token", "issuer", "subject", "email", "ext_agent_host_id", "scopes", "expires_in", "saved_at"
}


class Rejected(ValueError):
    pass


def receive(stream, consumed_dir: str, allowed_transaction: str, now: int | None = None) -> dict:
    data = stream.read(MAX_PAYLOAD_BYTES + 1)
    if len(data) > MAX_PAYLOAD_BYTES:
        raise Rejected("payload_too_large")
    try:
        payload = json.loads(data, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, Rejected):
        raise Rejected("invalid_payload") from None
    if not isinstance(payload, dict) or set(payload) != {"version", "transaction_id", "expires_at", "credentials"}:
        raise Rejected("invalid_payload")
    transaction_id = payload["transaction_id"]
    expiry = payload["expires_at"]
    credentials = payload["credentials"]
    now = int(time.time()) if now is None else now
    if type(payload["version"]) is not int or payload["version"] != 1 or transaction_id != allowed_transaction:
        raise Rejected("unauthorized_transaction")
    try:
        uuid.UUID(transaction_id)
    except (ValueError, TypeError, AttributeError):
        raise Rejected("invalid_payload") from None
    if type(expiry) is not int or not now < expiry <= now + MAX_TTL_SECONDS:
        raise Rejected("expired_or_invalid")
    if not isinstance(credentials, dict) or not REQUIRED_CREDENTIALS <= credentials.keys() or not credentials.keys() <= ALLOWED_CREDENTIALS:
        raise Rejected("invalid_credentials")
    if any(not isinstance(value, str) or not value or len(value) > 8192 for value in credentials.values()):
        raise Rejected("invalid_credentials")

    marker = os.path.join(consumed_dir, hashlib.sha256(transaction_id.encode()).hexdigest())
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Rejected("replay") from None
    with os.fdopen(fd, "w", encoding="ascii") as record:
        record.write("consumed\n")
    return {"status": "received", "transaction_id": transaction_id}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected("duplicate_key")
        result[key] = value
    return result


def main() -> int:
    try:
        with open("/run/allowed-transaction", encoding="ascii") as config:
            allowed_transaction = config.read(64).strip()
        result = receive(sys.stdin.buffer, "/run/consumed", allowed_transaction)
    except Rejected as error:
        print(json.dumps({"status": "rejected", "reason": str(error)}, separators=(",", ":")))
        return 2
    except OSError:
        print('{"status":"rejected","reason":"misconfigured"}')
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
