"""Temporary-database preparation for the future administrative store."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
from typing import Any, Callable, Iterator
import uuid

from prototypes.admin_auth import (
    ADMIN_PERMISSIONS,
    AuthFailure,
    LoginSession,
    Principal,
    _password_hash,
    _verify_password,
    authorize,
)


ADMIN_SCHEMA_VERSION = 1
NEXO_SCHEMA_VERSION = 28
LOGIN_SUBJECT = "administrator"
EVENT_TYPES = frozenset({
    "bootstrap_completed", "login_failed", "login_rate_limited", "login_succeeded",
    "session_expired", "logout", "session_revoked", "all_sessions_revoked",
    "password_reset_local",
})
AUTH_TABLES = frozenset({
    "auth_schema_version", "auth_state", "auth_users", "auth_user_permissions",
    "auth_sessions", "auth_login_guard", "auth_security_events",
})
AUTH_INDEXES = frozenset({
    "idx_auth_user_permissions_permission", "idx_auth_sessions_user",
    "idx_auth_sessions_expiry", "idx_auth_security_events_created",
})
AUTH_OBJECTS = AUTH_TABLES | AUTH_INDEXES
NEXO_REQUIRED_TABLES = frozenset({
    "providers", "models", "conversations", "messages", "notebooks", "notebook_sources",
    "canonical_documents", "document_chunks", "runtime_runs", "runtime_events",
    "runtime_interactions", "pending_approvals", "mcp_servers", "mcp_tools",
})
NEXO_REQUIRED_COLUMNS = {
    "schema_migrations": {"version", "applied_at"},
    "providers": {"id", "name", "base_url", "api_key", "created_at"},
    "models": {"id", "provider_id", "label", "capabilities"},
    "conversations": {"id", "title", "created_at", "updated_at", "execution_mode", "tools_enabled", "web_enabled"},
    "messages": {"id", "conversation_id", "role", "content", "attachments", "sources", "runtime_metadata", "artifacts"},
    "agent_profiles": {"id", "name", "provider_id", "model_id", "max_tool_calls"},
    "notebooks": {"id", "name", "created_at", "updated_at"},
    "notebook_sources": {"id", "notebook_id", "type", "status", "indexing_status"},
    "canonical_documents": {"id", "notebook_id", "source_id", "content", "content_hash"},
    "document_chunks": {"id", "notebook_id", "document_id", "source_id", "content"},
    "runtime_runs": {"id", "conversation_id", "message_id", "status"},
    "runtime_events": {"id", "run_id", "sequence", "kind", "status"},
    "runtime_interactions": {"id", "run_id", "conversation_id", "status", "expires_at"},
    "pending_approvals": {"id", "run_id", "conversation_id", "tool_fingerprint", "status"},
    "mcp_servers": {"id", "slug", "endpoint", "auth_type"},
    "mcp_tools": {"id", "server_id", "remote_name", "input_schema", "action", "read_only_hint"},
}
OWNED_TABLES = ("conversations", "notebooks")


class PersistenceError(Exception):
    """Base for sanitized adapter and backup failures."""


class PersistenceUnavailable(PersistenceError):
    pass


class IncompatibleSchema(PersistenceError):
    pass


class OwnershipRequired(PersistenceError):
    pass


class PersistenceConflict(PersistenceError):
    pass


class BackupFailure(PersistenceError):
    pass


class AdministrativePersistence:
    """SQLite auth adapter restricted to explicit files under the OS temp root.

    It never reads NEXO_DATA_DIR and is not imported by app.main. Production
    integration requires a separate reviewed host-side adapter and migration.
    """

    def __init__(self, path: str | Path, *, idle_ttl: int = 1800, absolute_ttl: int = 43200,
                 max_attempts: int = 5, lockout_seconds: int = 300,
                 failure_window: int = 900, timeout: float = 1.0) -> None:
        self.path = _temporary_database_path(path)
        if (idle_ttl < 1 or absolute_ttl < idle_ttl or max_attempts < 1
                or lockout_seconds < 1 or failure_window < 1 or timeout < 0):
            raise ValueError("invalid persistence policy")
        self.idle_ttl = idle_ttl
        self.absolute_ttl = absolute_ttl
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.failure_window = failure_window
        self.timeout = timeout

    @contextmanager
    def _connection(self, *, create: bool = False, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            if not self.path.exists() and create:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(descriptor)
            if not self.path.exists() and not create:
                raise PersistenceUnavailable("administrative storage unavailable")
            if self.path.exists() and not readonly:
                os.chmod(self.path, 0o600)
            database = self.path.as_uri() + "?mode=ro" if readonly else str(self.path)
            connection = sqlite3.connect(database, uri=readonly, timeout=self.timeout, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise PersistenceUnavailable("administrative storage unavailable")
            connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            yield connection
        except PersistenceError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise _translate_sqlite_error(error) from None
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _transaction(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        with self._connection(create=create) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _validate_nexo_schema(connection)
                yield connection
                connection.commit()
            except PersistenceError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _translate_sqlite_error(error) from None
            except Exception:
                connection.rollback()
                raise

    def initialize(self) -> None:
        """Create all auth objects atomically, or validate the complete schema."""
        with self._transaction(create=True) as connection:
            _validate_nexo_schema(connection)
            _assert_foreign_keys(connection)
            objects = _administrative_objects(connection)
            if objects and objects != AUTH_OBJECTS:
                raise IncompatibleSchema("administrative schema is partial or incompatible")
            if not objects:
                for statement in _AUTH_DDL:
                    connection.execute(statement)
                connection.execute("INSERT INTO auth_schema_version(id,version) VALUES(1,?)",
                                   (ADMIN_SCHEMA_VERSION,))
                connection.execute("INSERT INTO auth_state(id,initialized,policy_version,bootstrapped_at) VALUES(1,0,1,NULL)")
                connection.execute("INSERT INTO auth_login_guard(subject,failures,window_started_at,locked_until) VALUES(?,0,0,0)",
                                   (LOGIN_SUBJECT,))
            _validate_admin_schema(connection)
            _read_state(connection)
            _assert_foreign_keys(connection)
        os.chmod(self.path, 0o600)

    def verify(self) -> dict[str, Any]:
        with self._connection(readonly=True) as connection:
            _validate_nexo_schema(connection)
            _validate_admin_schema(connection)
            state = _read_state(connection)
            _assert_foreign_keys(connection)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise IncompatibleSchema("database integrity check failed")
            if state["initialized"]:
                _validate_ownership(connection)
            return {"integrity": integrity, "foreign_keys": True,
                    "initialized": state["initialized"], "schema_version": ADMIN_SCHEMA_VERSION}

    def status(self, now: float | None = None) -> dict[str, int | bool]:
        if not self.path.exists():
            return {"initialized": False, "active_sessions": 0}
        with self._connection(readonly=True) as connection:
            _validate_nexo_schema(connection)
            _validate_admin_schema(connection)
            state = _read_state(connection)
            timestamp = time.time() if now is None else now
            active = connection.execute("""SELECT COUNT(*) FROM auth_sessions
                WHERE revoked_at IS NULL AND idle_expires_at>? AND absolute_expires_at>?""",
                (timestamp, timestamp)).fetchone()[0]
            if state["initialized"]:
                _validate_ownership(connection)
            return {"initialized": state["initialized"], "active_sessions": active}

    def bootstrap_admin(self, username: str, password: str, *, permissions: frozenset[str] | None = None,
                        assign_historical: bool = False, now: float | None = None) -> str:
        normalized = _normalize_username(username)
        grants = ADMIN_PERMISSIONS if permissions is None else frozenset(permissions)
        if not grants or not grants.issubset(ADMIN_PERMISSIONS) or any("*" in item for item in grants):
            raise ValueError("invalid explicit permissions")
        encoded = _password_hash(password)
        timestamp = time.time() if now is None else now
        user_id = str(uuid.uuid4())
        with self._transaction() as connection:
            _validate_nexo_schema(connection)
            _validate_admin_schema(connection)
            state = _read_state(connection)
            if state["initialized"]:
                raise PersistenceConflict("bootstrap is already complete")
            ownership = _ownership_rows(connection)
            if any(count and not assign_historical for _table, count in ownership):
                raise OwnershipRequired("explicit historical ownership assignment is required")
            connection.execute("""INSERT INTO auth_users
                (id,username,username_normalized,password_hash,status,created_at,password_changed_at)
                VALUES(?,?,?,?, 'active', ?, ?)""",
                (user_id, username.strip(), normalized, encoded, timestamp, timestamp))
            connection.executemany("INSERT INTO auth_user_permissions(user_id,permission) VALUES(?,?)",
                                   ((user_id, grant) for grant in sorted(grants)))
            if assign_historical:
                for table, _count in ownership:
                    connection.execute(f"UPDATE {table} SET owner_user_id=? WHERE owner_user_id IS NULL", (user_id,))
            _validate_ownership(connection)
            connection.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) VALUES('bootstrap_completed',?,?)",
                               (user_id, timestamp))
            cursor = connection.execute("""UPDATE auth_state SET initialized=1,bootstrapped_at=?
                WHERE id=1 AND initialized=0""", (timestamp,))
            if cursor.rowcount != 1:
                raise PersistenceConflict("bootstrap state changed")
            _read_state(connection)
            _assert_foreign_keys(connection)
        return user_id

    def login(self, username: str, password: str, *, now: float | None = None) -> LoginSession:
        timestamp = time.time() if now is None else now
        normalized = _normalize_username(username)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).digest()
        csrf_hash = hashlib.sha256(csrf.encode()).digest()
        rejected = False
        with self._transaction() as connection:
            _validate_admin_schema(connection)
            state = _read_state(connection)
            guard = connection.execute("SELECT failures,window_started_at,locked_until FROM auth_login_guard WHERE subject=?",
                                       (LOGIN_SUBJECT,)).fetchone()
            admin = _bootstrap_user(connection) if state["initialized"] else None
            if guard is None:
                raise IncompatibleSchema("login guard state is missing")
            if not state["initialized"] or admin is None:
                rejected = True
            elif timestamp < guard["locked_until"]:
                rejected = True
            else:
                failures, window_started = guard["failures"], guard["window_started_at"]
                if timestamp - window_started >= self.failure_window:
                    failures, window_started = 0, timestamp
                password_matches = _verify_password(password, admin["password_hash"])
                valid = normalized == admin["username_normalized"] and admin["status"] == "active" and password_matches
                if not valid:
                    failures += 1
                    locked_until = timestamp + self.lockout_seconds if failures >= self.max_attempts else 0
                    connection.execute("UPDATE auth_login_guard SET failures=?,window_started_at=?,locked_until=? WHERE subject=?",
                                       (failures, window_started, locked_until, LOGIN_SUBJECT))
                    self._event(connection, "login_failed", None, timestamp)
                    if locked_until:
                        self._event(connection, "login_rate_limited", None, timestamp)
                    rejected = True
                else:
                    connection.execute("UPDATE auth_login_guard SET failures=0,window_started_at=?,locked_until=0 WHERE subject=?",
                                       (timestamp, LOGIN_SUBJECT))
                    connection.execute("""INSERT INTO auth_sessions
                        (token_hash,csrf_hash,user_id,created_at,last_seen_at,idle_expires_at,absolute_expires_at)
                        VALUES(?,?,?,?,?,?,?)""",
                        (token_hash, csrf_hash, admin["id"], timestamp, timestamp,
                         timestamp + self.idle_ttl, timestamp + self.absolute_ttl))
                    self._event(connection, "login_succeeded", admin["id"], timestamp)
        if rejected:
            raise AuthFailure("authentication_failed", 401)
        return LoginSession(token, csrf, min(timestamp + self.idle_ttl, timestamp + self.absolute_ttl))

    def authenticate(self, token: str, permission: str, *, now: float | None = None,
                     csrf_token: str | None = None, mutating: bool = False) -> Principal:
        timestamp = time.time() if now is None else now
        token_hash = hashlib.sha256(token.encode()).digest()
        expired = False
        principal: Principal | None = None
        with self._transaction() as connection:
            _validate_admin_schema(connection)
            _read_state(connection, require_initialized=True)
            row = connection.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise AuthFailure("authentication_required", 401)
            if timestamp >= row["idle_expires_at"] or timestamp >= row["absolute_expires_at"]:
                connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                                   (timestamp, token_hash))
                self._event(connection, "session_expired", row["user_id"], timestamp)
                expired = True
            else:
                if mutating:
                    supplied = hashlib.sha256((csrf_token or "").encode()).digest()
                    if not csrf_token or not hmac.compare_digest(supplied, row["csrf_hash"]):
                        raise AuthFailure("csrf_invalid", 403)
                user = connection.execute("SELECT username,status FROM auth_users WHERE id=?", (row["user_id"],)).fetchone()
                if user is None or user["status"] != "active":
                    raise AuthFailure("authentication_required", 401)
                grants = frozenset(item[0] for item in connection.execute(
                    "SELECT permission FROM auth_user_permissions WHERE user_id=?", (row["user_id"],)))
                principal = Principal("user", user["username"], grants)
                connection.execute("UPDATE auth_sessions SET last_seen_at=?,idle_expires_at=? WHERE token_hash=?",
                                   (timestamp, min(timestamp + self.idle_ttl, row["absolute_expires_at"]), token_hash))
                authorize(principal, permission, expected_kind="user")
        if expired:
            raise AuthFailure("authentication_required", 401)
        assert principal is not None
        return principal

    def issue_csrf_token(self, token: str, *, now: float | None = None) -> str:
        timestamp = time.time() if now is None else now
        csrf = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).digest()
        expired = False
        with self._transaction() as connection:
            _validate_admin_schema(connection)
            _read_state(connection, require_initialized=True)
            row = connection.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise AuthFailure("authentication_required", 401)
            if timestamp >= min(row["idle_expires_at"], row["absolute_expires_at"]):
                connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?", (timestamp, token_hash))
                self._event(connection, "session_expired", row["user_id"], timestamp)
                expired = True
            else:
                connection.execute("UPDATE auth_sessions SET csrf_hash=?,last_seen_at=?,idle_expires_at=? WHERE token_hash=?",
                                   (hashlib.sha256(csrf.encode()).digest(), timestamp,
                                    min(timestamp + self.idle_ttl, row["absolute_expires_at"]), token_hash))
        if expired:
            raise AuthFailure("authentication_required", 401)
        return csrf

    def logout(self, token: str, *, now: float | None = None) -> bool:
        return self.revoke_session(token, event_type="logout", now=now)

    def revoke_session(self, token: str, *, event_type: str = "session_revoked",
                       now: float | None = None) -> bool:
        if event_type not in {"logout", "session_revoked"}:
            raise ValueError("invalid session revocation event")
        timestamp = time.time() if now is None else now
        with self._transaction() as connection:
            _validate_admin_schema(connection)
            token_hash = hashlib.sha256(token.encode()).digest()
            row = connection.execute("SELECT user_id FROM auth_sessions WHERE token_hash=? AND revoked_at IS NULL",
                                     (token_hash,)).fetchone()
            if row is None:
                return False
            connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                               (timestamp, token_hash))
            self._event(connection, event_type, row["user_id"], timestamp)
        return True

    def revoke_all_sessions(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else now
        with self._transaction() as connection:
            _validate_admin_schema(connection)
            _read_state(connection, require_initialized=True)
            cursor = connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE revoked_at IS NULL", (timestamp,))
            if cursor.rowcount:
                user = _bootstrap_user(connection)
                self._event(connection, "all_sessions_revoked", user["id"] if user else None, timestamp)
            return cursor.rowcount

    def reset_password_local(self, password: str, *, now: float | None = None) -> None:
        encoded = _password_hash(password)
        timestamp = time.time() if now is None else now
        with self._transaction() as connection:
            _validate_admin_schema(connection)
            _read_state(connection, require_initialized=True)
            admin = _bootstrap_user(connection)
            if admin is None:
                raise IncompatibleSchema("bootstrap administrator is missing")
            connection.execute("UPDATE auth_users SET password_hash=?,password_changed_at=? WHERE id=?",
                               (encoded, timestamp, admin["id"]))
            connection.execute("UPDATE auth_sessions SET revoked_at=? WHERE revoked_at IS NULL", (timestamp,))
            connection.execute("UPDATE auth_login_guard SET failures=0,window_started_at=?,locked_until=0 WHERE subject=?",
                               (timestamp, LOGIN_SUBJECT))
            self._event(connection, "password_reset_local", admin["id"], timestamp)

    def security_events(self) -> list[tuple[str, str | None, float]]:
        with self._connection(readonly=True) as connection:
            _validate_nexo_schema(connection)
            _validate_admin_schema(connection)
            return [(row["event_type"], row["actor_user_id"], row["created_at"])
                    for row in connection.execute("SELECT event_type,actor_user_id,created_at FROM auth_security_events ORDER BY id")]

    def _event(self, connection: sqlite3.Connection, event_type: str, actor: str | None, timestamp: float) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError("invalid security event")
        connection.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) VALUES(?,?,?)",
                           (event_type, actor, timestamp))


_AUTH_DDL = (
    """CREATE TABLE auth_schema_version (
      id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL CHECK(version>0))""",
    """CREATE TABLE auth_state (
      id INTEGER PRIMARY KEY CHECK(id=1), initialized INTEGER NOT NULL CHECK(initialized IN (0,1)),
      policy_version INTEGER NOT NULL CHECK(policy_version>0), bootstrapped_at REAL,
      CHECK((initialized=0 AND bootstrapped_at IS NULL) OR (initialized=1 AND bootstrapped_at IS NOT NULL)))""",
    """CREATE TABLE auth_users (
      id TEXT PRIMARY KEY, username TEXT NOT NULL, username_normalized TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','disabled')),
      created_at REAL NOT NULL, password_changed_at REAL NOT NULL)""",
    """CREATE TABLE auth_user_permissions (
      user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
      permission TEXT NOT NULL CHECK(length(permission)>0 AND instr(permission,'*')=0),
      PRIMARY KEY(user_id,permission))""",
    """CREATE TABLE auth_sessions (
      token_hash BLOB PRIMARY KEY CHECK(length(token_hash)=32),
      csrf_hash BLOB NOT NULL CHECK(length(csrf_hash)=32),
      user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
      created_at REAL NOT NULL, last_seen_at REAL NOT NULL, idle_expires_at REAL NOT NULL,
      absolute_expires_at REAL NOT NULL, revoked_at REAL,
      CHECK(idle_expires_at<=absolute_expires_at), CHECK(revoked_at IS NULL OR revoked_at>=created_at))""",
    """CREATE TABLE auth_login_guard (
      subject TEXT PRIMARY KEY, failures INTEGER NOT NULL CHECK(failures>=0),
      window_started_at REAL NOT NULL, locked_until REAL NOT NULL CHECK(locked_until>=0))""",
    """CREATE TABLE auth_security_events (
      id INTEGER PRIMARY KEY, event_type TEXT NOT NULL CHECK(event_type IN
      ('bootstrap_completed','login_failed','login_rate_limited','login_succeeded','session_expired',
       'logout','session_revoked','all_sessions_revoked','password_reset_local')),
      actor_user_id TEXT REFERENCES auth_users(id) ON DELETE SET NULL,
      created_at REAL NOT NULL, correlation_id TEXT)""",
    "CREATE INDEX idx_auth_user_permissions_permission ON auth_user_permissions(permission,user_id)",
    "CREATE INDEX idx_auth_sessions_user ON auth_sessions(user_id,revoked_at)",
    "CREATE INDEX idx_auth_sessions_expiry ON auth_sessions(revoked_at,idle_expires_at,absolute_expires_at)",
    "CREATE INDEX idx_auth_security_events_created ON auth_security_events(created_at,id)",
)

_AUTH_COLUMNS = {
    "auth_schema_version": {"id", "version"},
    "auth_state": {"id", "initialized", "policy_version", "bootstrapped_at"},
    "auth_users": {"id", "username", "username_normalized", "password_hash", "status", "created_at", "password_changed_at"},
    "auth_user_permissions": {"user_id", "permission"},
    "auth_sessions": {"token_hash", "csrf_hash", "user_id", "created_at", "last_seen_at", "idle_expires_at", "absolute_expires_at", "revoked_at"},
    "auth_login_guard": {"subject", "failures", "window_started_at", "locked_until"},
    "auth_security_events": {"id", "event_type", "actor_user_id", "created_at", "correlation_id"},
}


def verify_sqlite_file(path: str | Path) -> dict[str, Any]:
    database = _temporary_database_path(path)
    if not database.is_file():
        raise BackupFailure("SQLite file unavailable")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1.0)
        connection.execute("PRAGMA foreign_keys=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or violations:
            raise BackupFailure("SQLite integrity verification failed")
        return {"integrity": integrity, "foreign_key_violations": len(violations)}
    except BackupFailure:
        raise
    except (sqlite3.Error, OSError):
        raise BackupFailure("SQLite integrity verification failed") from None
    finally:
        if connection is not None:
            connection.close()


def backup_database(source: str | Path, destination: str | Path, *, pages: int = -1,
                    progress: Callable[[int, int, int], None] | None = None) -> dict[str, Any]:
    return _copy_database_atomic(source, destination, pages=pages, progress=progress)


def restore_database(backup: str | Path, destination: str | Path, *, pages: int = -1,
                     progress: Callable[[int, int, int], None] | None = None) -> dict[str, Any]:
    return _copy_database_atomic(backup, destination, pages=pages, progress=progress)


def _copy_database_atomic(source: str | Path, destination: str | Path, *, pages: int,
                          progress: Callable[[int, int, int], None] | None) -> dict[str, Any]:
    source_path = _temporary_database_path(source)
    destination_path = _temporary_database_path(destination)
    if source_path == destination_path or pages == 0 or pages < -1:
        raise ValueError("invalid backup request")
    if not source_path.is_file() or destination_path.exists():
        raise BackupFailure("backup source/destination state rejected")
    staging = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.partial")
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=1.0)
        destination_connection = sqlite3.connect(str(staging), timeout=1.0, isolation_level=None)
        destination_connection.execute("PRAGMA foreign_keys=ON")
        source_connection.backup(destination_connection, pages=pages, progress=progress, sleep=0.01)
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        result = verify_sqlite_file(staging)
        os.chmod(staging, 0o600)
        os.link(staging, destination_path)
        staging.unlink()
        return result
    except BackupFailure:
        raise
    except Exception:
        raise BackupFailure("SQLite backup/restore failed") from None
    finally:
        if source_connection is not None:
            source_connection.close()
        if destination_connection is not None:
            destination_connection.close()
        _remove_sqlite_files(staging)


def _temporary_database_path(path: str | Path) -> Path:
    if os.fspath(path) == ":memory:":
        raise ValueError("a persistent temporary SQLite file is required")
    candidate = Path(path).expanduser().absolute()
    temporary_input = Path(tempfile.gettempdir()).absolute()
    temporary_root = temporary_input.resolve()
    relative = None
    for root in (temporary_input, temporary_root):
        try:
            relative = candidate.relative_to(root)
            temporary_input = root
            break
        except ValueError:
            pass
    if relative is None:
        raise ValueError("SQLite path must be under the temporary root") from None
    current = temporary_input
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("SQLite symlinks are not accepted")
    resolved = candidate.resolve()
    configured_data_dir = Path(os.environ.get("NEXO_DATA_DIR", "./data")).expanduser().resolve()
    if (not resolved.is_relative_to(temporary_root) or resolved == temporary_root
            or resolved.is_relative_to(configured_data_dir) or resolved.name == "nexo.sqlite3"):
        raise ValueError("SQLite path must be a non-production temporary file")
    if resolved.exists() and not resolved.is_file():
        raise ValueError("SQLite path must be a file")
    if not resolved.parent.is_dir():
        raise ValueError("SQLite parent directory must already exist")
    return resolved


def _translate_sqlite_error(error: BaseException) -> PersistenceError:
    if isinstance(error, sqlite3.IntegrityError):
        return PersistenceConflict("administrative transaction rejected")
    return PersistenceUnavailable("administrative storage unavailable")


def _administrative_objects(connection: sqlite3.Connection) -> frozenset[str]:
    rows = connection.execute("""SELECT name FROM sqlite_master
        WHERE name LIKE 'auth_%' OR name LIKE 'idx_auth_%'""").fetchall()
    return frozenset(row[0] for row in rows)


def _validate_nexo_schema(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_ledger = "schema_migrations" in tables
    domain_tables = tables - AUTH_TABLES
    if not has_ledger:
        if domain_tables:
            raise IncompatibleSchema("NEXO schema ledger is missing")
        return
    versions = tuple(row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version"))
    if versions != tuple(range(1, NEXO_SCHEMA_VERSION + 1)):
        raise IncompatibleSchema("NEXO schema version is unsupported")
    if not NEXO_REQUIRED_TABLES.issubset(tables):
        raise IncompatibleSchema("NEXO schema is incomplete")
    for table, required in NEXO_REQUIRED_COLUMNS.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required.issubset(columns):
            raise IncompatibleSchema("NEXO schema is incomplete")


def _validate_admin_schema(connection: sqlite3.Connection) -> None:
    objects = _administrative_objects(connection)
    if objects != AUTH_OBJECTS:
        raise IncompatibleSchema("administrative schema is partial or incompatible")
    for table, required in _AUTH_COLUMNS.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required.issubset(columns):
            raise IncompatibleSchema("administrative schema is partial or incompatible")
    version = connection.execute("SELECT version FROM auth_schema_version WHERE id=1").fetchone()
    if version is None or version[0] != ADMIN_SCHEMA_VERSION:
        raise IncompatibleSchema("administrative schema version is unsupported")
    if connection.execute("SELECT COUNT(*) FROM auth_state WHERE id=1").fetchone()[0] != 1:
        raise IncompatibleSchema("administrative state is missing")
    if connection.execute("SELECT COUNT(*) FROM auth_login_guard WHERE subject=?", (LOGIN_SUBJECT,)).fetchone()[0] != 1:
        raise IncompatibleSchema("administrative rate limit state is missing")
    _require_foreign_key(connection, "auth_user_permissions", "user_id", "auth_users")
    _require_foreign_key(connection, "auth_sessions", "user_id", "auth_users")
    _require_foreign_key(connection, "auth_security_events", "actor_user_id", "auth_users")


def _read_state(connection: sqlite3.Connection, *, require_initialized: bool = False) -> sqlite3.Row:
    state = connection.execute("SELECT * FROM auth_state WHERE id=1").fetchone()
    if state is None or state["policy_version"] != 1:
        raise IncompatibleSchema("administrative state is incompatible")
    users = connection.execute("SELECT id,status FROM auth_users ORDER BY id").fetchall()
    permission_rows = connection.execute("SELECT user_id,permission FROM auth_user_permissions").fetchall()
    permissions = {}
    for row in permission_rows:
        if row["permission"] not in ADMIN_PERMISSIONS or "*" in row["permission"]:
            raise IncompatibleSchema("administrative permissions are incompatible")
        permissions.setdefault(row["user_id"], set()).add(row["permission"])
    bootstrap = connection.execute("""SELECT actor_user_id FROM auth_security_events
        WHERE event_type='bootstrap_completed' ORDER BY id LIMIT 1""").fetchone()
    if not state["initialized"]:
        if users or permission_rows or state["bootstrapped_at"] is not None or bootstrap is not None:
            raise IncompatibleSchema("uninitialized administrative state contains identity data")
        if require_initialized:
            raise AuthFailure("authentication_required", 401)
        return state
    bootstrap_user = next((user for user in users if bootstrap and user["id"] == bootstrap["actor_user_id"]), None)
    if (state["bootstrapped_at"] is None or bootstrap_user is None or bootstrap_user["status"] != "active"
            or not permissions.get(bootstrap_user["id"])
            or not any(user["status"] == "active" and permissions.get(user["id"]) for user in users)):
        raise IncompatibleSchema("initialized administrative state is incomplete")
    _validate_ownership(connection)
    return state


def _bootstrap_user(connection: sqlite3.Connection) -> sqlite3.Row | None:
    event = connection.execute("""SELECT actor_user_id FROM auth_security_events
        WHERE event_type='bootstrap_completed' ORDER BY id LIMIT 1""").fetchone()
    if event is None or not event["actor_user_id"]:
        return None
    return connection.execute("SELECT * FROM auth_users WHERE id=?", (event["actor_user_id"],)).fetchone()


def _ownership_rows(connection: sqlite3.Connection) -> list[tuple[str, int]]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = []
    for table in OWNED_TABLES:
        if table not in tables:
            continue
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "owner_user_id" not in columns:
            raise OwnershipRequired(f"experimental {table}.owner_user_id is required before bootstrap")
        _require_foreign_key(connection, table, "owner_user_id", "auth_users")
        count = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_user_id IS NULL").fetchone()[0]
        result.append((table, count))
    return result


def _validate_ownership(connection: sqlite3.Connection) -> None:
    if any(count for _table, count in _ownership_rows(connection)):
        raise IncompatibleSchema("initialized database contains unowned private records")


def _require_foreign_key(connection: sqlite3.Connection, table: str, column: str, target: str) -> None:
    relations = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    if not any(row["from"] == column and row["table"] == target for row in relations):
        raise IncompatibleSchema("required foreign key is missing")


def _assert_foreign_keys(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise IncompatibleSchema("foreign key enforcement is disabled")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise IncompatibleSchema("foreign key integrity check failed")


def _normalize_username(username: str) -> str:
    if not isinstance(username, str) or not username.strip() or len(username) > 120:
        raise ValueError("invalid administrator name")
    return username.strip().casefold()


def _remove_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
