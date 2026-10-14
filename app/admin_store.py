"""Administrative persistence over the canonical NEXO SQLite schema."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import secrets
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Callable
import uuid

from app import safe_migrations
from app.safe_migrations import CURRENT_SCHEMA_VERSION, DatabaseLease


# CP2-B.2's documented minimum; the final route permission catalog is unresolved.
PASSWORD_VERSION = 1
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_BYTES = 1024
LOGIN_SUBJECT = "administrator"
CP2_B2_MINIMUM_PERMISSIONS = frozenset({
    "config:read", "config:write", "chat:read", "chat:write",
})


class AdminStoreError(Exception):
    """Base class for sanitized administrative-store failures."""


class AdminStorageUnavailable(AdminStoreError):
    pass


class AdminSchemaUnavailable(AdminStoreError):
    pass


class BootstrapUnavailable(AdminStoreError):
    pass


class BootstrapBlocked(AdminStoreError):
    pass


class AuthenticationFailure(AdminStoreError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class LoginSession:
    token: str
    csrf_token: str
    expires_at: float


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    permissions: frozenset[str]


def _password_hash(password: str) -> str:
    if not isinstance(password, str):
        raise ValueError("password policy rejected")
    raw = password.encode("utf-8")
    if len(password) < MIN_PASSWORD_CHARS or len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError("password policy rejected")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(raw, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"scrypt${PASSWORD_VERSION}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, version, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if (algorithm != "scrypt" or int(version) != PASSWORD_VERSION
                or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)):
            return False
        raw, salt, expected = password.encode("utf-8"), bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if len(raw) > MAX_PASSWORD_BYTES or len(salt) != 16 or len(expected) != SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(raw, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def _normalize_username(username: str) -> str:
    if not isinstance(username, str) or not username.strip() or len(username) > 120:
        raise ValueError("invalid administrator name")
    return username.strip().casefold()


class AdminStore:
    """Uses v31 tables only; callers must hold this DB's shared or exclusive lease."""

    def __init__(self, database: str | Path, lease: DatabaseLease, *, idle_ttl: int = 1800,
                 absolute_ttl: int = 43200, max_attempts: int = 5,
                 lockout_seconds: int = 300, failure_window: int = 900,
                 clock: Callable[[], float] = time.time) -> None:
        self.database = Path(database).absolute()
        self.lease = lease
        if (self.database != lease.database or lease._fd is None
                or idle_ttl < 1 or absolute_ttl < idle_ttl or max_attempts < 1
                or lockout_seconds < 1 or failure_window < 1):
            raise ValueError("an active matching database lease and valid policy are required")
        self.idle_ttl = idle_ttl
        self.absolute_ttl = absolute_ttl
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.failure_window = failure_window
        self.clock = clock

    def _check_lease(self) -> None:
        if self.lease._fd is None or self.lease.database != self.database:
            raise AdminStorageUnavailable("administrative storage unavailable")

    @contextmanager
    def _connection(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            self._check_lease()
            if self.database.is_symlink() or not self.database.is_file():
                raise AdminStorageUnavailable("administrative storage unavailable")
            journal = self.database.parent / safe_migrations.JOURNAL_NAME
            if journal.exists() or journal.is_symlink():
                raise AdminSchemaUnavailable("canonical NEXO recovery is required")
            if any(Path(str(self.database) + suffix).exists() or Path(str(self.database) + suffix).is_symlink()
                   for suffix in ("-wal", "-shm")):
                raise AdminSchemaUnavailable("canonical NEXO database has unsupported sidecars")
            mode = "ro" if readonly else "rw"
            connection = sqlite3.connect(
                self.database.as_uri() + f"?mode={mode}", uri=True, timeout=5, isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise AdminStorageUnavailable("administrative storage unavailable")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise AdminSchemaUnavailable("canonical NEXO database must use DELETE journal mode")
            yield connection
        except (AdminStorageUnavailable, AdminSchemaUnavailable):
            raise
        except safe_migrations.SafeMigrationError:
            raise AdminSchemaUnavailable("canonical NEXO database is unavailable") from None
        except sqlite3.IntegrityError:
            raise AdminStoreError("administrative transaction rejected") from None
        except (sqlite3.Error, OSError):
            raise AdminStorageUnavailable("administrative storage unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        try:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if version != CURRENT_SCHEMA_VERSION:
                raise AdminSchemaUnavailable("canonical NEXO schema v31 is required")
            preferences = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_preferences'"
            ).fetchone()
            safe_migrations.validate_schema_manifest(
                connection,
                safe_migrations.expected_historical_audit_schema_manifest(bool(preferences)),
            )
            safe_migrations._validate_admin_data(connection)
            policy = connection.execute("SELECT policy_version FROM auth_state WHERE id=1").fetchone()
            if policy is None or policy[0] != 1:
                raise AdminSchemaUnavailable("administrative policy version is unsupported")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise AdminSchemaUnavailable("canonical NEXO database is incompatible")
            permissions = connection.execute("SELECT permission FROM auth_user_permissions").fetchall()
            if any(row[0] not in CP2_B2_MINIMUM_PERMISSIONS or "*" in row[0] for row in permissions):
                raise AdminSchemaUnavailable("administrative grants are incompatible")
        except AdminStoreError:
            raise
        except (sqlite3.Error, safe_migrations.SafeMigrationError, ValueError):
            raise AdminSchemaUnavailable("canonical NEXO database is incompatible") from None

    @contextmanager
    def _transaction(self, *, exclusive: bool = False) -> Iterator[sqlite3.Connection]:
        if exclusive and not self.lease.exclusive:
            raise BootstrapUnavailable("bootstrap requires the exclusive database lease")
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._validate(connection)
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._connection(readonly=True) as connection:
            try:
                connection.execute("BEGIN")
                self._validate(connection)
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _event(connection: sqlite3.Connection, event_type: str, actor_user_id: str | None,
               timestamp: float, correlation_id: str | None = None) -> None:
        connection.execute(
            "INSERT INTO auth_security_events(event_type,actor_user_id,created_at,correlation_id) VALUES(?,?,?,?)",
            (event_type, actor_user_id, timestamp, correlation_id),
        )

    def bootstrap_admin(self, username: str, password: str, *,
                        permissions: frozenset[str] = CP2_B2_MINIMUM_PERMISSIONS) -> str:
        if not self.lease.exclusive:
            raise BootstrapUnavailable("bootstrap requires the exclusive database lease")
        try:
            safe_migrations._require_delete_journal_source(self.database)
        except safe_migrations.SafeMigrationError:
            raise AdminSchemaUnavailable("offline bootstrap preconditions are not met") from None
        normalized = _normalize_username(username)
        grants = frozenset(permissions)
        if not grants or not grants.issubset(CP2_B2_MINIMUM_PERMISSIONS) or any("*" in grant for grant in grants):
            raise ValueError("invalid explicit permissions")
        encoded = _password_hash(password)
        timestamp, user_id, operation_id = self.clock(), str(uuid.uuid4()), str(uuid.uuid4())
        with self._transaction(exclusive=True) as connection:
            state = connection.execute("SELECT state FROM auth_state WHERE id=1").fetchone()
            if state is None or state["state"] != "schema_installed":
                raise BootstrapUnavailable("bootstrap is not available")
            if any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                   for table in ("conversations", "notebooks", "auth_historical_ownership_operations")):
                raise BootstrapBlocked("existing user data requires the reviewed offline ownership transition")
            connection.execute("UPDATE auth_state SET state='bootstrap_pending' WHERE id=1 AND state='schema_installed'")
            connection.execute("""INSERT INTO auth_users
                (id,username,username_normalized,password_hash,status,created_at,password_changed_at)
                VALUES(?,?,?,?,'active',?,?)""",
                (user_id, username.strip(), normalized, encoded, timestamp, timestamp))
            connection.executemany(
                "INSERT INTO auth_user_permissions(user_id,permission) VALUES(?,?)",
                ((user_id, grant) for grant in sorted(grants)),
            )
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_preferences'").fetchone():
                connection.execute("""INSERT INTO auth_user_preferences(user_id,key,value,updated_at)
                    SELECT ?,key,value,updated_at FROM user_preferences""", (user_id,))
            self._event(connection, "bootstrap_completed", user_id, timestamp, operation_id)
            connection.execute("""UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at=?
                WHERE id=1 AND state='bootstrap_pending'""", (str(timestamp),))
            self._validate(connection)
        return user_id

    def login(self, username: str, password: str) -> LoginSession:
        normalized = _normalize_username(username)
        if not isinstance(password, str):
            raise AuthenticationFailure("authentication_failed", 401)
        timestamp = self.clock()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        token_hash, csrf_hash = hashlib.sha256(token.encode()).digest(), hashlib.sha256(csrf.encode()).digest()
        rejected = False
        expires_at = timestamp
        with self._transaction() as connection:
            state = connection.execute("SELECT state FROM auth_state WHERE id=1").fetchone()
            guard = connection.execute("""SELECT failures,window_started_at,locked_until
                FROM auth_login_guard WHERE subject=?""", (LOGIN_SUBJECT,)).fetchone()
            if guard is None:
                raise AdminSchemaUnavailable("administrative login guard is unavailable")
            user = connection.execute("SELECT * FROM auth_users WHERE username_normalized=?", (normalized,)).fetchone()
            if state is None or state["state"] != "bootstrap_completed":
                rejected = True
            elif timestamp < guard["locked_until"]:
                self._event(connection, "login_rate_limited", None, timestamp)
                rejected = True
            else:
                failures, window_started = guard["failures"], guard["window_started_at"]
                if timestamp - window_started >= self.failure_window:
                    failures, window_started = 0, timestamp
                valid = bool(user and user["status"] == "active" and _verify_password(password, user["password_hash"]))
                if not valid:
                    failures += 1
                    locked_until = timestamp + self.lockout_seconds if failures >= self.max_attempts else 0
                    connection.execute("""UPDATE auth_login_guard SET failures=?,window_started_at=?,locked_until=?
                        WHERE subject=?""", (failures, window_started, locked_until, LOGIN_SUBJECT))
                    self._event(connection, "login_failed", None, timestamp)
                    if locked_until:
                        self._event(connection, "login_rate_limited", None, timestamp)
                    rejected = True
                else:
                    connection.execute("UPDATE auth_login_guard SET failures=0,window_started_at=?,locked_until=0 WHERE subject=?",
                                       (timestamp, LOGIN_SUBJECT))
                    expires_at = min(timestamp + self.idle_ttl, timestamp + self.absolute_ttl)
                    connection.execute("""INSERT INTO auth_sessions
                        (token_hash,csrf_hash,user_id,created_at,last_seen_at,idle_expires_at,absolute_expires_at)
                        VALUES(?,?,?,?,?,?,?)""",
                        (token_hash, csrf_hash, user["id"], timestamp, timestamp,
                         timestamp + self.idle_ttl, timestamp + self.absolute_ttl))
                    self._event(connection, "login_succeeded", user["id"], timestamp)
        if rejected:
            raise AuthenticationFailure("authentication_failed", 401)
        return LoginSession(token, csrf, expires_at)

    def authenticate(self, token: str, permission: str, *, csrf_token: str | None = None,
                     mutating: bool = False) -> Principal:
        if not token or not isinstance(token, str):
            raise AuthenticationFailure("authentication_required", 401)
        if permission not in CP2_B2_MINIMUM_PERMISSIONS:
            raise AuthenticationFailure("permission_denied", 403)
        timestamp, token_hash = self.clock(), hashlib.sha256(token.encode()).digest()
        principal = None
        expired = False
        with self._transaction() as connection:
            session = connection.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if session is None or session["revoked_at"] is not None:
                raise AuthenticationFailure("authentication_required", 401)
            if timestamp >= min(session["idle_expires_at"], session["absolute_expires_at"]):
                connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                                   (timestamp, token_hash))
                self._event(connection, "session_expired", session["user_id"], timestamp)
                expired = True
            else:
                if mutating:
                    supplied = hashlib.sha256((csrf_token or "").encode()).digest()
                    if not csrf_token or not hmac.compare_digest(supplied, session["csrf_hash"]):
                        raise AuthenticationFailure("csrf_invalid", 403)
                user = connection.execute("SELECT id,username,status FROM auth_users WHERE id=?",
                                          (session["user_id"],)).fetchone()
                if user is None or user["status"] != "active":
                    raise AuthenticationFailure("authentication_required", 401)
                grants = frozenset(row[0] for row in connection.execute(
                    "SELECT permission FROM auth_user_permissions WHERE user_id=?", (user["id"],)))
                if permission not in grants:
                    raise AuthenticationFailure("permission_denied", 403)
                connection.execute("UPDATE auth_sessions SET last_seen_at=?,idle_expires_at=? WHERE token_hash=?",
                                   (timestamp, min(timestamp + self.idle_ttl, session["absolute_expires_at"]), token_hash))
                principal = Principal(user["id"], user["username"], grants)
        if expired:
            raise AuthenticationFailure("authentication_required", 401)
        assert principal is not None
        return principal

    def rotate_csrf_token(self, token: str) -> str:
        if not token or not isinstance(token, str):
            raise AuthenticationFailure("authentication_required", 401)
        timestamp, token_hash = self.clock(), hashlib.sha256(token.encode()).digest()
        csrf = secrets.token_urlsafe(32)
        expired = False
        with self._transaction() as connection:
            session = connection.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if session is None or session["revoked_at"] is not None:
                raise AuthenticationFailure("authentication_required", 401)
            if timestamp >= min(session["idle_expires_at"], session["absolute_expires_at"]):
                connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                                   (timestamp, token_hash))
                self._event(connection, "session_expired", session["user_id"], timestamp)
                expired = True
            else:
                connection.execute("""UPDATE auth_sessions SET csrf_hash=?,last_seen_at=?,idle_expires_at=?
                    WHERE token_hash=?""",
                    (hashlib.sha256(csrf.encode()).digest(), timestamp,
                     min(timestamp + self.idle_ttl, session["absolute_expires_at"]), token_hash))
        if expired:
            raise AuthenticationFailure("authentication_required", 401)
        return csrf

    def logout(self, token: str) -> bool:
        return self.revoke_session(token, event_type="logout")

    def revoke_session(self, token: str, *, event_type: str = "session_revoked") -> bool:
        if not isinstance(token, str) or not token:
            raise AuthenticationFailure("authentication_required", 401)
        if event_type not in {"logout", "session_revoked"}:
            raise ValueError("invalid session event")
        timestamp, token_hash = self.clock(), hashlib.sha256(token.encode()).digest()
        with self._transaction() as connection:
            session = connection.execute("SELECT user_id FROM auth_sessions WHERE token_hash=? AND revoked_at IS NULL",
                                         (token_hash,)).fetchone()
            if session is None:
                return False
            connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                               (timestamp, token_hash))
            self._event(connection, event_type, session["user_id"], timestamp)
        return True

    def revoke_all_sessions(self, user_id: str) -> int:
        timestamp = self.clock()
        with self._transaction() as connection:
            if not connection.execute("SELECT 1 FROM auth_users WHERE id=?", (user_id,)).fetchone():
                raise AuthenticationFailure("authentication_required", 401)
            cursor = connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                                        (timestamp, user_id))
            if cursor.rowcount:
                self._event(connection, "all_sessions_revoked", user_id, timestamp)
            return cursor.rowcount

    def status(self) -> dict[str, int | str]:
        timestamp = self.clock()
        with self._read() as connection:
            state = connection.execute("SELECT state FROM auth_state WHERE id=1").fetchone()[0]
            active = connection.execute("""SELECT COUNT(*) FROM auth_sessions
                WHERE revoked_at IS NULL AND idle_expires_at>? AND absolute_expires_at>?""",
                (timestamp, timestamp)).fetchone()[0]
            return {"state": state, "active_sessions": active}
