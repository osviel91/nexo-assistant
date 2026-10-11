"""Standalone encrypted credential-store proof of concept; never used by NEXO."""

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAX_PAYLOAD_BYTES = 16 * 1024
REQUIRED_FIELDS = {"client_id", "access_token", "refresh_token"}
OPTIONAL_FIELDS = {"id_token", "issuer", "subject", "email", "ext_agent_host_id", "scopes", "expires_at"}
AAD_PREFIX = b"nexo-credential-poc:v1:"


class StoreError(ValueError):
    pass


class CredentialStore:
    def __init__(self, database: str, key_file: str) -> None:
        self.database = database
        key_path = Path(key_file)
        if key_path.resolve() == Path(database).resolve():
            raise StoreError("key_must_be_external")
        if os.name == "posix" and key_path.stat().st_mode & 0o077:
            raise StoreError("key_permissions_rejected")
        key = key_path.read_bytes()
        if len(key) != 32:
            raise StoreError("key_invalid")
        self._cipher = AESGCM(key)
        with self._connect() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS authorized_imports (
                    transaction_id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY, transaction_id TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','active')),
                    expires_at INTEGER, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL,
                    fingerprint TEXT NOT NULL
                );
            """)
        if os.name == "posix":
            os.chmod(database, 0o600)

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=5, isolation_level="IMMEDIATE")
        db.execute("PRAGMA secure_delete=ON")
        return db

    def authorize_import(self, transaction_id: str, expires_at: int, admin: str, now: int) -> None:
        if admin != "admin" or not _uuid(transaction_id) or type(expires_at) is not int or not now < expires_at <= now + 300:
            raise StoreError("import_not_authorized")
        with self._connect() as db:
            try:
                db.execute("INSERT INTO authorized_imports VALUES(?,?)", (transaction_id, expires_at))
            except sqlite3.IntegrityError:
                raise StoreError("import_not_authorized") from None

    def stage(self, transaction_id: str, credentials: dict, now: int) -> tuple[str, str]:
        _validate_credentials(credentials)
        if not _uuid(transaction_id):
            raise StoreError("invalid_payload")
        serialized = json.dumps(credentials, separators=(",", ":"), sort_keys=True).encode()
        if len(serialized) > MAX_PAYLOAD_BYTES:
            raise StoreError("payload_too_large")
        credential_id = str(uuid.uuid4())
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, serialized, AAD_PREFIX + credential_id.encode())
        fingerprint = hashlib.sha256(ciphertext).hexdigest()
        try:
            with self._connect() as db:
                row = db.execute("SELECT transaction_id FROM authorized_imports WHERE transaction_id=? AND expires_at>?",
                                 (transaction_id, now)).fetchone()
                if row is None:
                    raise StoreError("import_not_authorized")
                db.execute("DELETE FROM authorized_imports WHERE transaction_id=?", (transaction_id,))
                db.execute("INSERT INTO credentials VALUES(?,?,'pending',?,?,?,?)",
                           (credential_id, transaction_id, credentials.get("expires_at"), nonce, ciphertext, fingerprint))
        except sqlite3.IntegrityError:
            raise StoreError("import_not_authorized") from None
        return credential_id, fingerprint

    def confirm(self, credential_id: str, fingerprint: str, admin: str) -> None:
        if admin != "admin":
            raise StoreError("confirmation_not_authorized")
        with self._connect() as db:
            cursor = db.execute("UPDATE credentials SET state='active' WHERE id=? AND state='pending' AND fingerprint=?",
                                (credential_id, fingerprint))
            if cursor.rowcount != 1:
                raise StoreError("confirmation_unavailable")

    def status(self, credential_id: str, now: int) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT state,expires_at,fingerprint FROM credentials WHERE id=?", (credential_id,)).fetchone()
        if row is None:
            return {"status": "absent"}
        state, expiry, fingerprint = row
        return {"status": "expired" if expiry is not None and expiry <= now else state,
                "fingerprint": fingerprint}

    def get_credentials(self, credential_id: str, component: str, now: int) -> dict:
        if component != "credential-provider":
            raise StoreError("component_not_authorized")
        with self._connect() as db:
            row = db.execute("SELECT state,expires_at,nonce,ciphertext FROM credentials WHERE id=?", (credential_id,)).fetchone()
        if row is None or row[0] != "active" or row[1] is not None and row[1] <= now:
            raise StoreError("credential_unavailable")
        try:
            plaintext = self._cipher.decrypt(row[2], row[3], AAD_PREFIX + credential_id.encode())
            return json.loads(plaintext)
        except Exception:
            raise StoreError("credential_unavailable") from None

    def delete(self, credential_id: str, admin: str) -> None:
        if admin != "admin":
            raise StoreError("deletion_not_authorized")
        with self._connect() as db:
            db.execute("DELETE FROM credentials WHERE id=?", (credential_id,))

    def diagnostics(self) -> dict[str, int]:
        with self._connect() as db:
            counts = dict(db.execute("SELECT state,COUNT(*) FROM credentials GROUP BY state").fetchall())
        return {"pending": counts.get("pending", 0), "active": counts.get("active", 0)}


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _validate_credentials(credentials: dict) -> None:
    if not isinstance(credentials, dict) or not REQUIRED_FIELDS <= credentials.keys() or not credentials.keys() <= REQUIRED_FIELDS | OPTIONAL_FIELDS:
        raise StoreError("invalid_credentials")
    if any(not isinstance(value, str) or not value or len(value) > 8192
           for key, value in credentials.items() if key != "expires_at"):
        raise StoreError("invalid_credentials")
    if "expires_at" in credentials and (type(credentials["expires_at"]) is not int or credentials["expires_at"] <= 0):
        raise StoreError("invalid_credentials")
