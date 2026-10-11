"""Persistent, isolated administrative authentication core for Stage 15B.3B-1.

This module is deliberately not imported by app.main and must use a dedicated
SQLite file, never NEXO's production database.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from getpass import getpass
from pathlib import Path
from typing import Iterator


logger = logging.getLogger("nexo.auth.prototype")
PASSWORD_VERSION = 1
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_BYTES = 1024

ADMIN_PERMISSIONS = frozenset({
    "config:read", "config:write", "providers:read", "providers:write", "providers:test",
    "secrets:metadata", "secrets:write", "secrets:rotate", "agents:read", "agents:write",
    "agents:run", "tools:read", "tools:write", "tools:approve", "mcp:read", "mcp:write",
    "mcp:connect", "rag:read", "rag:read_content", "rag:write", "rag:query",
    "rag:diagnostics", "chat:read", "chat:write", "chat:delete", "diagnostics:read",
    "diagnostics:run",
})
SERVICE_SCOPES = {
    "agent-runtime": frozenset({"provider:invoke", "tool:invoke", "rag:query"}),
    "retrieval": frozenset({"embedding:invoke", "rag:query"}),
}


class AuthFailure(Exception):
    """Safe, non-secret-bearing failure with an HTTP contract status."""

    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class AuthUnavailable(AuthFailure):
    def __init__(self) -> None:
        super().__init__("authentication_unavailable", 503)


class BootstrapComplete(AuthFailure):
    def __init__(self) -> None:
        super().__init__("bootstrap_unavailable", 409)


@dataclass(frozen=True)
class Principal:
    kind: str
    subject: str
    permissions: frozenset[str]
    delegation_id: str | None = None


@dataclass(frozen=True)
class LoginSession:
    token: str
    csrf_token: str
    expires_at: float


def _password_hash(password: str) -> str:
    raw = password.encode("utf-8")
    if len(password) < MIN_PASSWORD_CHARS or len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError("password policy rejected")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(raw, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"scrypt${PASSWORD_VERSION}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, version, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt" or int(version) != PASSWORD_VERSION:
            return False
        params = (int(n), int(r), int(p))
        if params != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        raw = password.encode("utf-8")
        if len(raw) > MAX_PASSWORD_BYTES:
            return False
        expected = bytes.fromhex(digest_hex)
        salt = bytes.fromhex(salt_hex)
        if len(salt) != 16 or len(expected) != SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(raw, salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                                p=SCRYPT_P, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


class AdminAuthStore:
    """SQLite store for an isolated auth database; no app migration is applied."""

    def __init__(self, path: str | Path, *, idle_ttl: int = 1800, absolute_ttl: int = 43200,
                 max_attempts: int = 5, lockout_seconds: int = 300,
                 failure_window: int = 900) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("authentication prototype requires a dedicated persistent SQLite file")
        db_file = Path(self.path)
        if db_file.is_symlink():
            raise ValueError("authentication prototype refuses SQLite symlinks")
        resolved_path = db_file.resolve()
        configured_db = (Path(os.getenv("NEXO_DATA_DIR", "./data")) / "nexo.sqlite3").resolve()
        if resolved_path.name == "nexo.sqlite3" or resolved_path == configured_db:
            raise ValueError("authentication prototype requires a dedicated SQLite file")
        if (idle_ttl < 1 or absolute_ttl < idle_ttl or max_attempts < 1
                or lockout_seconds < 1 or failure_window < 1):
            raise ValueError("invalid authentication policy")
        self.idle_ttl = idle_ttl
        self.absolute_ttl = absolute_ttl
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.failure_window = failure_window

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            db_file = Path(self.path)
            db_file.parent.mkdir(parents=True, exist_ok=True)
            if db_file.exists():
                os.chmod(db_file, 0o600)
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            os.chmod(db_file, 0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        except (sqlite3.Error, OSError) as error:
            if connection is not None:
                connection.rollback()
            logger.warning("auth_storage_error")
            raise AuthUnavailable() from error
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS auth_schema_version (
              id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO auth_schema_version(id,version) VALUES(1,1);
            CREATE TABLE IF NOT EXISTS auth_state (
              id INTEGER PRIMARY KEY CHECK(id=1), initialized INTEGER NOT NULL CHECK(initialized IN (0,1))
            );
            INSERT OR IGNORE INTO auth_state(id,initialized) VALUES(1,0);
            CREATE TABLE IF NOT EXISTS auth_admin (
              id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL, created_at REAL NOT NULL, password_changed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
              token_hash BLOB PRIMARY KEY, csrf_hash BLOB NOT NULL,
              created_at REAL NOT NULL, last_seen_at REAL NOT NULL,
              idle_expires_at REAL NOT NULL, absolute_expires_at REAL NOT NULL,
              revoked_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry
              ON auth_sessions(revoked_at,idle_expires_at,absolute_expires_at);
            CREATE TABLE IF NOT EXISTS auth_login_guard (
              id INTEGER PRIMARY KEY CHECK(id=1), failures INTEGER NOT NULL,
              window_started_at REAL NOT NULL, locked_until REAL NOT NULL
            );
            INSERT OR IGNORE INTO auth_login_guard(id,failures,window_started_at,locked_until)
              VALUES(1,0,0,0);
            CREATE TABLE IF NOT EXISTS auth_security_events (
              id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, created_at REAL NOT NULL
            );
            """)
            version = connection.execute("SELECT version FROM auth_schema_version WHERE id=1").fetchone()
            if version is None or version["version"] != 1:
                raise AuthUnavailable()

    @staticmethod
    def _event(connection: sqlite3.Connection, event_type: str, now: float) -> None:
        connection.execute("INSERT INTO auth_security_events(event_type,created_at) VALUES(?,?)",
                           (event_type, now))

    def bootstrap_local(self, username: str, password: str, now: float | None = None) -> None:
        if not username or len(username) > 120:
            raise ValueError("invalid administrator name")
        encoded = _password_hash(password)
        timestamp = time.time() if now is None else now
        with self._transaction() as connection:
            state = connection.execute("SELECT initialized FROM auth_state WHERE id=1").fetchone()
            if state is None or state["initialized"]:
                raise BootstrapComplete()
            connection.execute("INSERT INTO auth_admin(id,username,password_hash,created_at,password_changed_at) VALUES(1,?,?,?,?)",
                               (username, encoded, timestamp, timestamp))
            connection.execute("UPDATE auth_state SET initialized=1 WHERE id=1 AND initialized=0")
            self._event(connection, "bootstrap_completed", timestamp)
        logger.info("auth_event=bootstrap_completed")

    def login(self, username: str, password: str, now: float | None = None) -> LoginSession:
        timestamp = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).digest()
        csrf_hash = hashlib.sha256(csrf.encode()).digest()
        rejected = False
        with self._transaction() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE revoked_at IS NOT NULL OR idle_expires_at<=? OR absolute_expires_at<=?",
                               (timestamp, timestamp))
            state = connection.execute("SELECT initialized FROM auth_state WHERE id=1").fetchone()
            admin = connection.execute("SELECT username,password_hash FROM auth_admin WHERE id=1").fetchone()
            guard = connection.execute("SELECT failures,window_started_at,locked_until FROM auth_login_guard WHERE id=1").fetchone()
            if state is None or not state["initialized"] or admin is None or guard is None:
                rejected = True
            elif timestamp < guard["locked_until"]:
                rejected = True
            else:
                failures, window_start = guard["failures"], guard["window_started_at"]
                if timestamp - window_start >= self.failure_window:
                    failures, window_start = 0, timestamp
                password_matches = _verify_password(password, admin["password_hash"])
                valid = username == admin["username"] and password_matches
                if not valid:
                    failures += 1
                    locked_until = timestamp + self.lockout_seconds if failures >= self.max_attempts else 0
                    connection.execute("UPDATE auth_login_guard SET failures=?,window_started_at=?,locked_until=? WHERE id=1",
                                       (failures, window_start, locked_until))
                    self._event(connection, "login_failed", timestamp)
                    if locked_until:
                        self._event(connection, "login_rate_limited", timestamp)
                    rejected = True
                else:
                    connection.execute("UPDATE auth_login_guard SET failures=0,window_started_at=?,locked_until=0 WHERE id=1",
                                       (timestamp,))
                    connection.execute("INSERT INTO auth_sessions(token_hash,csrf_hash,created_at,last_seen_at,idle_expires_at,absolute_expires_at) VALUES(?,?,?,?,?,?)",
                                       (token_hash, csrf_hash, timestamp, timestamp,
                                        timestamp + self.idle_ttl, timestamp + self.absolute_ttl))
                    self._event(connection, "login_succeeded", timestamp)
        if rejected:
            raise AuthFailure("authentication_failed", 401)
        logger.info("auth_event=login_succeeded")
        return LoginSession(token, csrf, min(timestamp + self.idle_ttl, timestamp + self.absolute_ttl))

    def authenticate(self, token: str, permission: str, *, now: float | None = None,
                     csrf_token: str | None = None, mutating: bool = False) -> Principal:
        timestamp = time.time() if now is None else now
        token_hash = hashlib.sha256(token.encode()).digest()
        expired = False
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise AuthFailure("authentication_required", 401)
            if timestamp >= row["idle_expires_at"] or timestamp >= row["absolute_expires_at"]:
                connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                                   (timestamp, token_hash))
                self._event(connection, "session_expired", timestamp)
                expired = True
            else:
                admin = connection.execute("SELECT username FROM auth_admin WHERE id=1").fetchone()
                if admin is None:
                    raise AuthUnavailable()
                if mutating:
                    supplied = hashlib.sha256((csrf_token or "").encode()).digest()
                    if not csrf_token or not hmac.compare_digest(supplied, row["csrf_hash"]):
                        raise AuthFailure("csrf_invalid", 403)
                connection.execute("UPDATE auth_sessions SET last_seen_at=?,idle_expires_at=? WHERE token_hash=?",
                                   (timestamp, min(timestamp + self.idle_ttl, row["absolute_expires_at"]), token_hash))
                principal = Principal("user", admin["username"], ADMIN_PERMISSIONS)
                authorize(principal, permission, expected_kind="user")
        if expired:
            logger.info("auth_event=session_expired")
            raise AuthFailure("authentication_required", 401)
        return principal

    def issue_csrf_token(self, token: str, now: float | None = None) -> str:
        timestamp = time.time() if now is None else now
        csrf = secrets.token_urlsafe(32)
        expired = False
        with self._transaction() as connection:
            token_hash = hashlib.sha256(token.encode()).digest()
            row = connection.execute("SELECT idle_expires_at,absolute_expires_at,revoked_at FROM auth_sessions WHERE token_hash=?",
                                     (token_hash,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise AuthFailure("authentication_required", 401)
            if timestamp >= min(row["idle_expires_at"], row["absolute_expires_at"]):
                connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?", (timestamp, token_hash))
                self._event(connection, "session_expired", timestamp)
                expired = True
            else:
                connection.execute("UPDATE auth_sessions SET csrf_hash=?,last_seen_at=?,idle_expires_at=? WHERE token_hash=?",
                                   (hashlib.sha256(csrf.encode()).digest(), timestamp,
                                    min(timestamp + self.idle_ttl, row["absolute_expires_at"]), token_hash))
        if expired:
            logger.info("auth_event=session_expired")
            raise AuthFailure("authentication_required", 401)
        return csrf

    def logout(self, token: str, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._transaction() as connection:
            connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                               (timestamp, hashlib.sha256(token.encode()).digest()))
            self._event(connection, "logout", timestamp)
        logger.info("auth_event=logout")

    def revoke_session(self, token: str, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._transaction() as connection:
            cursor = connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                                        (timestamp, hashlib.sha256(token.encode()).digest()))
            if cursor.rowcount:
                self._event(connection, "session_revoked", timestamp)
        logger.info("auth_event=session_revoked")

    def revoke_all(self, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._transaction() as connection:
            connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE revoked_at IS NULL", (timestamp,))
            self._event(connection, "all_sessions_revoked", timestamp)
        logger.info("auth_event=all_sessions_revoked")

    def reset_password_local(self, password: str, now: float | None = None) -> None:
        encoded = _password_hash(password)
        timestamp = time.time() if now is None else now
        with self._transaction() as connection:
            cursor = connection.execute("UPDATE auth_admin SET password_hash=?,password_changed_at=? WHERE id=1",
                                        (encoded, timestamp))
            if not cursor.rowcount:
                raise BootstrapComplete()
            connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE revoked_at IS NULL", (timestamp,))
            connection.execute("UPDATE auth_login_guard SET failures=0,window_started_at=?,locked_until=0 WHERE id=1",
                               (timestamp,))
            self._event(connection, "password_reset_local", timestamp)
        logger.info("auth_event=password_reset_local")

    def security_events(self) -> list[tuple[str, float]]:
        with self._connection() as connection:
            return [(row["event_type"], row["created_at"]) for row in connection.execute(
                "SELECT event_type,created_at FROM auth_security_events ORDER BY id")]

    def session_count(self) -> int:
        with self._connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM auth_sessions WHERE revoked_at IS NULL").fetchone()[0]


def authorize(principal: Principal, permission: str, *, expected_kind: str) -> None:
    if principal.kind != expected_kind:
        raise AuthFailure("principal_type_mismatch", 403)
    if permission not in principal.permissions:
        raise AuthFailure("permission_denied", 403)


def service_principal(service_id: str, scopes: frozenset[str], delegation_id: str) -> Principal:
    allowed = SERVICE_SCOPES.get(service_id, frozenset())
    if not service_id or not delegation_id or not scopes or not scopes.issubset(allowed):
        raise AuthFailure("service_scope_denied", 403)
    return Principal("service", service_id, scopes, delegation_id)


def bootstrap_from_console(store: AdminAuthStore) -> None:
    """Provision the first admin from a local terminal; never expose through HTTP."""
    store.initialize()
    username = input("Administrator username: ").strip()
    password = getpass("Administrator passphrase: ")
    confirmation = getpass("Confirm passphrase: ")
    if not hmac.compare_digest(password.encode(), confirmation.encode()):
        raise ValueError("passphrases do not match")
    store.bootstrap_local(username, password)
