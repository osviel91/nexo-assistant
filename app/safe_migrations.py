"""Offline SQLite migration, validation, and single-file promotion for NEXO."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import uuid

from app import migrations


HISTORICAL_SCHEMA_VERSION = 28
CURRENT_SCHEMA_VERSION = 29
LOCK_SUFFIX = ".migration.lock"
JOURNAL_NAME = ".nexo.sqlite3.promotion.json"
FAULT_HOOK = Callable[[str], None]


class SafeMigrationError(RuntimeError):
    pass


class DatabaseInUse(SafeMigrationError):
    pass


class SchemaCompatibilityError(SafeMigrationError):
    pass


class MigrationRecoveryRequired(SafeMigrationError):
    pass


@dataclass(frozen=True)
class DatabaseState:
    version: int
    user_preferences: bool
    schema_digest: str
    data_digest: str


@dataclass(frozen=True)
class SchemaObject:
    kind: str
    name: str
    table_name: str
    sql: str | None


@dataclass(frozen=True)
class TableStructure:
    name: str
    columns: tuple[tuple, ...]
    indexes: tuple[tuple, ...]
    foreign_keys: tuple[tuple, ...]


@dataclass(frozen=True)
class SchemaManifest:
    objects: tuple[SchemaObject, ...]
    permitted_extensions: tuple[SchemaObject, ...]
    tables: tuple[TableStructure, ...]
    extension_tables: tuple[TableStructure, ...]


@dataclass(frozen=True)
class MigrationResult:
    source_version: int
    target_version: int
    backup_path: Path | None
    changed: bool


@dataclass(frozen=True)
class TransactionalMigration:
    version: int
    statements: tuple[str, ...]
    validate_previous: Callable[[sqlite3.Connection], None]
    validate_output: Callable[[sqlite3.Connection], None]


ADMIN_SCHEMA_STATEMENTS = (
    "CREATE TABLE auth_state (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL CHECK(state IN ('schema_installed','bootstrap_pending','bootstrap_completed')), policy_version INTEGER NOT NULL CHECK(policy_version>0), bootstrapped_at TEXT, CHECK((state IN ('schema_installed','bootstrap_pending') AND bootstrapped_at IS NULL) OR (state='bootstrap_completed' AND bootstrapped_at IS NOT NULL)))",
    "INSERT INTO auth_state(id,state,policy_version,bootstrapped_at) VALUES(1,'schema_installed',1,NULL)",
    "CREATE TABLE auth_users (id TEXT PRIMARY KEY, username TEXT NOT NULL, username_normalized TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL CHECK(length(password_hash)>0), status TEXT NOT NULL CHECK(status IN ('active','disabled')), created_at REAL NOT NULL, password_changed_at REAL NOT NULL)",
    "CREATE TABLE auth_user_permissions (user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE, permission TEXT NOT NULL CHECK(length(permission)>0 AND instr(permission,'*')=0), PRIMARY KEY(user_id,permission))",
    "CREATE INDEX idx_auth_user_permissions_permission ON auth_user_permissions(permission,user_id)",
    "CREATE TABLE auth_sessions (token_hash BLOB PRIMARY KEY CHECK(length(token_hash)=32), csrf_hash BLOB NOT NULL CHECK(length(csrf_hash)=32), user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE, created_at REAL NOT NULL, last_seen_at REAL NOT NULL, idle_expires_at REAL NOT NULL, absolute_expires_at REAL NOT NULL, revoked_at REAL, CHECK(idle_expires_at<=absolute_expires_at), CHECK(revoked_at IS NULL OR revoked_at>=created_at))",
    "CREATE INDEX idx_auth_sessions_user ON auth_sessions(user_id,revoked_at)",
    "CREATE INDEX idx_auth_sessions_expiry ON auth_sessions(revoked_at,idle_expires_at,absolute_expires_at)",
    "CREATE TABLE auth_login_guard (subject TEXT PRIMARY KEY, failures INTEGER NOT NULL CHECK(failures>=0), window_started_at REAL NOT NULL, locked_until REAL NOT NULL CHECK(locked_until>=0))",
    "INSERT INTO auth_login_guard(subject,failures,window_started_at,locked_until) VALUES('administrator',0,0,0)",
    "CREATE TABLE auth_security_events (id INTEGER PRIMARY KEY, event_type TEXT NOT NULL CHECK(event_type IN ('bootstrap_completed','login_failed','login_rate_limited','login_succeeded','session_expired','logout','session_revoked','all_sessions_revoked','password_reset_local')), actor_user_id TEXT REFERENCES auth_users(id) ON DELETE SET NULL, created_at REAL NOT NULL, correlation_id TEXT)",
    "CREATE INDEX idx_auth_security_events_created ON auth_security_events(created_at,id)",
    "CREATE TRIGGER auth_security_events_no_update BEFORE UPDATE ON auth_security_events BEGIN SELECT RAISE(ABORT, 'security events are append-only'); END",
    "CREATE TRIGGER auth_security_events_no_delete BEFORE DELETE ON auth_security_events BEGIN SELECT RAISE(ABORT, 'security events are append-only'); END",
)


def _admin_schema_manifest(connection: sqlite3.Connection, user_preferences: bool) -> SchemaManifest:
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version, migration in migrations.MIGRATIONS:
        migration(connection)
        connection.execute("INSERT INTO schema_migrations VALUES(?, 'manifest')", (version,))
    connection.execute("INSERT INTO schema_migrations VALUES(29, 'manifest')")
    for statement in ADMIN_SCHEMA_STATEMENTS:
        connection.execute(statement)
    if user_preferences:
        connection.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL)")
    return capture_schema_manifest(connection)


@lru_cache(maxsize=2)
def expected_admin_schema_manifest(user_preferences: bool = False) -> SchemaManifest:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        return _admin_schema_manifest(connection, user_preferences)
    finally:
        connection.close()


def _validate_admin_schema(connection: sqlite3.Connection) -> None:
    preferences = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_preferences'").fetchone()
    validate_schema_manifest(connection, expected_admin_schema_manifest(bool(preferences)))
    rows = connection.execute("SELECT state,bootstrapped_at FROM auth_state WHERE id=1").fetchall()
    if len(rows) != 1:
        raise SchemaCompatibilityError("administrative initialization state is missing or duplicated")
    state, bootstrapped_at = rows[0]
    if state not in {"schema_installed", "bootstrap_pending", "bootstrap_completed"}:
        raise SchemaCompatibilityError("administrative initialization state is unknown")
    if (state == "bootstrap_completed") != (bootstrapped_at is not None):
        raise SchemaCompatibilityError("administrative initialization state is inconsistent")
    guards = connection.execute("SELECT subject,failures,window_started_at,locked_until FROM auth_login_guard").fetchall()
    if (len(guards) != 1 or guards[0][0] != "administrator"
            or any(value < 0 for value in guards[0][1:])):
        raise SchemaCompatibilityError("administrative login guard is missing or invalid")
    if state != "bootstrap_completed" and any(connection.execute(
            f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("auth_users", "auth_user_permissions", "auth_sessions", "auth_security_events")):
        raise SchemaCompatibilityError("unbootstrapped administrative state contains identity data")
    if state == "bootstrap_completed":
        bootstrap = connection.execute("""SELECT actor_user_id FROM auth_security_events
            WHERE event_type='bootstrap_completed'""").fetchall()
        if (len(bootstrap) != 1 or not bootstrap[0][0]
                or not connection.execute("SELECT 1 FROM auth_users WHERE id=? AND status='active'",
                                          (bootstrap[0][0],)).fetchone()
                or not connection.execute("SELECT 1 FROM auth_user_permissions WHERE user_id=?",
                                          (bootstrap[0][0],)).fetchone()):
            raise SchemaCompatibilityError("completed administrative bootstrap is incomplete")


def _expected_manifest(version: int, user_preferences: bool = False) -> SchemaManifest:
    if version == CURRENT_SCHEMA_VERSION:
        return expected_admin_schema_manifest(user_preferences)
    return expected_schema_manifest(version, user_preferences)


TRANSACTIONAL_MIGRATIONS: tuple[TransactionalMigration, ...] = (
    TransactionalMigration(29, ADMIN_SCHEMA_STATEMENTS,
                           lambda db: validate_schema_manifest(db, expected_schema_manifest(
                               28, bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='user_preferences'").fetchone()))),
                           _validate_admin_schema),
)


class DatabaseLease:
    """An app holds SH for its lifetime; the offline engine requires EX."""

    def __init__(self, database: str | Path, *, exclusive: bool):
        self.database = Path(database).absolute()
        self.path = self.database.with_name(self.database.name + LOCK_SUFFIX)
        self.exclusive = exclusive
        self._fd: int | None = None

    def acquire(self) -> DatabaseLease:
        if os.name != "posix" or not self.path.parent.is_dir() or self.path.is_symlink():
            raise SafeMigrationError("SQLite maintenance locking is unavailable")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._fd = os.open(self.path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(self._fd).st_mode):
                raise SafeMigrationError("SQLite maintenance lock is not a regular file")
            os.fchmod(self._fd, 0o600)
            operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
            fcntl.flock(self._fd, operation | fcntl.LOCK_NB)
        except (OSError, SafeMigrationError) as error:
            self.close()
            if isinstance(error, SafeMigrationError):
                raise
            raise DatabaseInUse("database has an active NEXO runtime or migrator") from None
        return self

    def close(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> DatabaseLease:
        return self.acquire()

    def __exit__(self, *_args) -> None:
        self.close()


def acquire_runtime_lease(database: str | Path) -> DatabaseLease:
    return DatabaseLease(database, exclusive=False).acquire()


def validate_runtime_database(database: str | Path) -> DatabaseState:
    path = Path(database).absolute()
    if _journal_path(path).exists() or _journal_path(path).is_symlink():
        raise MigrationRecoveryRequired("offline migration recovery is incomplete")
    state = inspect_database(path, fingerprint_data=False)
    if state.version not in (HISTORICAL_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION):
        raise SchemaCompatibilityError("database schema is not the supported runtime version")
    return state


def inspect_database(database: str | Path, *, fingerprint_data: bool = True) -> DatabaseState:
    path = Path(database).absolute()
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise SchemaCompatibilityError("SQLite database file is unavailable or empty")
    try:
        connection = _connect_readonly(path)
        try:
            return _inspect_connection(connection, fingerprint_data=fingerprint_data)
        finally:
            connection.close()
    except SafeMigrationError:
        raise
    except sqlite3.Error:
        raise SchemaCompatibilityError("SQLite database cannot be inspected") from None


@lru_cache(maxsize=64)
def expected_schema_manifest(version: int, user_preferences: bool = False) -> SchemaManifest:
    if type(version) is not int or not 0 <= version <= HISTORICAL_SCHEMA_VERSION:
        raise SchemaCompatibilityError("schema version is unsupported")
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        if version:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            selected = tuple((number, migration) for number, migration in migrations.MIGRATIONS
                             if number <= version)
            if tuple(number for number, _ in selected) != tuple(range(1, version + 1)):
                raise SchemaCompatibilityError("historical migration registry is not contiguous")
            for number, migration in selected:
                migration(connection)
                connection.execute("INSERT INTO schema_migrations VALUES(?, 'manifest')", (number,))
        if user_preferences:
            connection.execute("CREATE TABLE IF NOT EXISTS user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL)")
        allowed = {("table", "user_preferences")} if user_preferences else set()
        return capture_schema_manifest(connection, allowed)
    except SafeMigrationError:
        raise
    except sqlite3.Error:
        raise SchemaCompatibilityError("could not construct trusted schema manifest") from None
    finally:
        connection.close()


def capture_schema_manifest(connection: sqlite3.Connection,
                            permitted_additional: Iterable[tuple[str, str]] = ()) -> SchemaManifest:
    """Capture a trusted code/fixture schema; never bless a candidate database."""
    permitted = frozenset(permitted_additional)
    objects = tuple(SchemaObject(row[0], row[1], row[2], _normalize_sql(row[3]))
                    for row in connection.execute("""SELECT type,name,tbl_name,sql FROM sqlite_master
                        WHERE type IN ('table','index','view','trigger') AND name NOT GLOB 'sqlite_*'
                        ORDER BY type,name"""))
    object_ids = {(item.kind, item.name) for item in objects}
    if not permitted.issubset(object_ids):
        raise ValueError("permitted extensions must exist in the trusted schema source")
    expected_objects = tuple(item for item in objects if (item.kind, item.name) not in permitted)
    extension_objects = tuple(item for item in objects if (item.kind, item.name) in permitted)
    table_names = {item.name for item in objects if item.kind == "table"}
    expected_tables = {item.name for item in expected_objects if item.kind == "table"}
    extension_tables = {item.name for item in extension_objects if item.kind == "table"}
    structures = {name: _table_structure(connection, name) for name in table_names}
    return SchemaManifest(expected_objects, extension_objects,
                          tuple(structures[name] for name in sorted(expected_tables)),
                          tuple(structures[name] for name in sorted(extension_tables)))


def validate_schema_manifest(connection: sqlite3.Connection, expected: SchemaManifest) -> None:
    permitted = tuple((item.kind, item.name) for item in expected.permitted_extensions)
    try:
        actual = capture_schema_manifest(connection, permitted)
    except ValueError:
        raise SchemaCompatibilityError("schema objects do not match the supported manifest") from None
    if actual != expected:
        raise SchemaCompatibilityError("schema objects do not match the supported manifest")


def _normalize_sql(sql: str | None) -> str | None:
    """Ignore SQL whitespace/comments and keyword case, but preserve quoted values."""
    if sql is None:
        return None
    tokens: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise SchemaCompatibilityError("unterminated schema SQL comment")
            index = end + 2
            continue
        if char in "'\"`[":
            closing = "]" if char == "[" else char
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == closing:
                    if closing != "]" and index + 1 < len(sql) and sql[index + 1] == closing:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise SchemaCompatibilityError("unterminated quoted schema SQL token")
            tokens.append(sql[start:index])
            continue
        if char.isalnum() or char in "_$":
            start = index
            index += 1
            while index < len(sql) and (sql[index].isalnum() or sql[index] in "_$"):
                index += 1
            tokens.append(sql[start:index].upper())
            continue
        operator = next((item for item in ("->>", "<=", ">=", "<>", "!=", "==", "||", "->")
                         if sql.startswith(item, index)), None)
        if operator:
            tokens.append(operator)
            index += len(operator)
        else:
            tokens.append(char)
            index += 1
    return "\x1f".join(tokens)


def _apply_transactional_migration(connection: sqlite3.Connection, lease: DatabaseLease, version: int,
                                   statements: Iterable[str], validate_previous: Callable,
                                   validate_output: Callable, fault_hook: Callable[[str], None] | None = None) -> bool:
    """Internal future-version contract; only a held offline directory lease may call it."""
    if (not isinstance(lease, DatabaseLease) or not lease.exclusive or lease._fd is None or type(version) is not int
            or version <= HISTORICAL_SCHEMA_VERSION or connection.in_transaction):
        raise DatabaseInUse("new migrations require the active offline engine lease")
    database_file = connection.execute("PRAGMA database_list").fetchone()[2]
    if not database_file or Path(database_file).resolve().parent != lease.database.resolve().parent:
        raise DatabaseInUse("migration connection is outside the leased data directory")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SchemaCompatibilityError("foreign-key enforcement must be enabled before migration")

    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_ledger(connection)
        versions = tuple(row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"))
        if versions == tuple(range(1, version + 1)):
            validate_output(connection)
            _require_database_integrity(connection)
            connection.commit()
            return False
        if versions != tuple(range(1, version)):
            raise SchemaCompatibilityError("new migration ledger has a gap or unknown version")

        validate_previous(connection)
        _require_database_integrity(connection)
        if fault_hook:
            fault_hook("before_migration")
        for index, statement in enumerate(statements, start=1):
            connection.execute(statement)
            if fault_hook:
                fault_hook(f"after_statement:{index}")
        validate_output(connection)
        _require_database_integrity(connection)
        if fault_hook:
            fault_hook("before_ledger")
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, datetime('now'))",
                           (version,))
        if fault_hook:
            fault_hook("after_ledger")
        connection.commit()
        if fault_hook:
            fault_hook("after_commit")
        return True
    except BaseException:
        connection.rollback()
        raise


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_structure(connection: sqlite3.Connection, name: str) -> TableStructure:
    quoted = _quote_identifier(name)
    columns = tuple(tuple(row[1:]) for row in connection.execute(f"PRAGMA table_xinfo({quoted})"))
    indexes = []
    for row in connection.execute(f"PRAGMA index_list({quoted})"):
        index_name = row[1]
        index_quoted = _quote_identifier(index_name)
        details = tuple(tuple(index_row) for index_row in
                        connection.execute(f"PRAGMA index_xinfo({index_quoted})"))
        indexes.append((index_name, row[2], row[3], row[4], details))
    foreign_keys = tuple(tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({quoted})"))
    return TableStructure(name, columns, tuple(sorted(indexes)), foreign_keys)


class SafeMigrationEngine:
    """Run the published history only on staging; atomically replace the canonical DB file."""

    def __init__(self, data_dir: str | Path):
        directory = Path(data_dir).expanduser().absolute()
        if directory.is_symlink() or not directory.is_dir():
            raise SafeMigrationError("data directory must exist and must not be a symlink")
        self.data_dir = directory.resolve()
        self.database = self.data_dir / "nexo.sqlite3"
        if self.database.is_symlink():
            raise SafeMigrationError("database symlinks are not accepted")

    def migrate(self, fault_hook: Callable[[str], None] | None = None) -> MigrationResult:
        with DatabaseLease(self.database, exclusive=True) as offline_lease:
            self._recover_interrupted_locked()
            self._cleanup_orphan_staging()
            if self.database.is_symlink():
                raise SchemaCompatibilityError("database symlinks are not accepted")
            if self.database.exists() and self.database.stat().st_size == 0:
                raise SchemaCompatibilityError("zero-byte database is not a recognized fresh state")
            source_exists = self.database.exists()
            if source_exists:
                _require_delete_journal_source(self.database)
                source = inspect_database(self.database, fingerprint_data=False)
                if source.version == CURRENT_SCHEMA_VERSION:
                    return MigrationResult(source.version, source.version, None, False)
                _require_delete_journal_source(self.database)
            else:
                source = DatabaseState(0, False, "", "")

            backup = self._unique_path("backup") if source_exists else None
            staging = self._unique_path("stage")
            if backup:
                self._backup_database(self.database, backup)
                backup_state = inspect_database(backup)
                if (backup_state.version, backup_state.schema_digest) != (source.version, source.schema_digest):
                    raise SafeMigrationError("backup schema changed during snapshot")
                source = backup_state
                if fault_hook:
                    fault_hook("after_backup")
                _backup_sqlite(backup, staging)
                os.chmod(staging, 0o600)
                _fsync_file(staging)
            else:
                _create_empty_database(staging)

            promoted = False
            marker_written = False
            source_connection: sqlite3.Connection | None = None
            try:
                stage_connection = sqlite3.connect(staging, timeout=5, isolation_level=None)
                try:
                    stage_connection.execute("PRAGMA foreign_keys=ON")
                    migrations.migrate(stage_connection)
                    for spec in TRANSACTIONAL_MIGRATIONS:
                        _apply_transactional_migration(
                            stage_connection, offline_lease, spec.version, spec.statements,
                            spec.validate_previous, spec.validate_output, fault_hook)
                finally:
                    stage_connection.close()
                _fsync_file(staging)
                validate_runtime_database(staging)
                target = inspect_database(staging)
                _require_standalone_file(staging)
                if fault_hook:
                    fault_hook("after_staging")

                if source_exists:
                    source_connection = self._prepare_exclusive_source(source)
                elif self.database.exists():
                    raise DatabaseInUse("database appeared during fresh staging")

                journal = {
                    "source_version": source.version,
                    "source_digest": source.data_digest,
                    "target_version": target.version,
                    "target_digest": target.data_digest,
                    "backup": backup.name if backup else None,
                    "staging": staging.name,
                }
                _write_journal(_journal_path(self.database), journal)
                marker_written = True
                if fault_hook:
                    fault_hook("before_replace")
                if source_exists and _data_fingerprint(
                source_connection, _expected_manifest(source.version, source.user_preferences)) != source.data_digest:
                    raise DatabaseInUse("database changed after backup")
                if not source_exists and self.database.exists():
                    raise DatabaseInUse("database appeared during fresh staging")

                os.replace(staging, self.database)
                promoted = True
                _fsync_directory(self.data_dir)
                if fault_hook:
                    fault_hook("after_replace")
                final = inspect_database(self.database)
                if (final.version, final.data_digest) != (target.version, target.data_digest):
                    raise MigrationRecoveryRequired("promoted database does not match validated staging")
                if fault_hook:
                    fault_hook("after_validation")
                _journal_path(self.database).unlink()
                _fsync_directory(self.data_dir)
                marker_written = False
                return MigrationResult(source.version, final.version, backup, True)
            except BaseException:
                if not promoted:
                    if marker_written:
                        _journal_path(self.database).unlink(missing_ok=True)
                        _fsync_directory(self.data_dir)
                    staging.unlink(missing_ok=True)
                    _remove_sidecars(staging)
                raise
            finally:
                if source_connection is not None:
                    source_connection.rollback()
                    source_connection.close()

    def recover(self) -> str:
        with DatabaseLease(self.database, exclusive=True):
            return self._recover_interrupted_locked()

    def _recover_interrupted_locked(self) -> str:
        journal_path = _journal_path(self.database)
        if journal_path.is_symlink():
            raise MigrationRecoveryRequired("migration journal may not be a symlink")
        if not journal_path.exists():
            return "clean"
        try:
            journal = json.loads(journal_path.read_text())
            source_version = journal["source_version"]
            source_digest = journal["source_digest"]
            target_version = journal["target_version"]
            target_digest = journal["target_digest"]
            backup_name = journal.get("backup")
            staging_name = journal["staging"]
            if (type(source_version) is not int or type(target_version) is not int
                    or source_version < 0 or target_version <= source_version
                    or not isinstance(source_digest, str) or not isinstance(target_digest, str)
                    or not target_digest or source_digest == target_digest):
                raise ValueError("migration generations are ambiguous")
            if (backup_name is None) != (source_version == 0 and source_digest == ""):
                raise ValueError("migration backup does not match source generation")
            backup = _artifact_path(self.data_dir, backup_name, "backup")
            staging = _artifact_path(self.data_dir, staging_name, "stage")
            if staging is None:
                raise ValueError("migration staging artifact is missing")
        except (OSError, ValueError, KeyError, TypeError):
            raise MigrationRecoveryRequired("migration journal is malformed") from None

        if backup is not None:
            if not backup.is_file():
                raise MigrationRecoveryRequired("original backup is missing")
            try:
                backup_state = inspect_database(backup)
            except SafeMigrationError:
                raise MigrationRecoveryRequired("original backup is invalid") from None
            if (backup_state.version, backup_state.data_digest) != (source_version, source_digest):
                raise MigrationRecoveryRequired("original backup does not match migration journal")

        if self.database.exists():
            try:
                state = inspect_database(self.database)
            except SafeMigrationError:
                state = None
            if state and (state.version, state.data_digest) == (target_version, target_digest):
                journal_path.unlink()
                _fsync_directory(self.data_dir)
                staging.unlink(missing_ok=True)
                _remove_sidecars(staging)
                return "promotion-completed"
            if state and (state.version, state.data_digest) == (source_version, source_digest):
                journal_path.unlink()
                _fsync_directory(self.data_dir)
                staging.unlink(missing_ok=True)
                _remove_sidecars(staging)
                return "promotion-not-started"
        elif source_version == 0 and source_digest == "":
            journal_path.unlink()
            _fsync_directory(self.data_dir)
            staging.unlink(missing_ok=True)
            _remove_sidecars(staging)
            return "promotion-not-started"

        raise MigrationRecoveryRequired("database matches neither original nor promoted manifest")

    def _prepare_exclusive_source(self, expected: DatabaseState) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            _require_delete_journal_source(self.database)
            connection = sqlite3.connect(self.database, timeout=0.25, isolation_level=None)
            connection.execute("PRAGMA foreign_keys=ON")
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
            if mode != "delete":
                raise DatabaseInUse("source must already use DELETE journal mode")
            _require_no_sidecars(self.database)
            connection.execute("BEGIN EXCLUSIVE")
            current = _inspect_connection(connection, fingerprint_data=True)
            if (current.version, current.data_digest) != (expected.version, expected.data_digest):
                raise DatabaseInUse("database changed after staging snapshot")
            wal = Path(str(self.database) + "-wal")
            shm = Path(str(self.database) + "-shm")
            journal = Path(str(self.database) + "-journal")
            if wal.exists() or shm.exists() or journal.exists():
                raise DatabaseInUse("source acquired a SQLite sidecar during promotion preflight")
            return connection
        except SafeMigrationError:
            if connection is not None:
                connection.rollback()
                connection.close()
            raise
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
                connection.close()
            raise DatabaseInUse("source has active connections or is locked") from None

    def _backup_database(self, source: Path, destination: Path) -> None:
        _backup_sqlite(source, destination)
        if destination.is_symlink() or not destination.is_file():
            raise SafeMigrationError("SQLite backup was not created as a regular file")
        os.chmod(destination, 0o600)
        _fsync_file(destination)
        _fsync_directory(self.data_dir)

    def _unique_path(self, kind: str) -> Path:
        return self.data_dir / f".nexo.sqlite3.{kind}.{uuid.uuid4().hex}.sqlite3"

    def _cleanup_orphan_staging(self) -> None:
        for path in self.data_dir.glob(".nexo.sqlite3.stage.*.sqlite3"):
            if path.is_file() and not path.is_symlink():
                path.unlink()
                _remove_sidecars(path)


def _inspect_connection(connection: sqlite3.Connection, *, fingerprint_data: bool) -> DatabaseState:
    connection.execute("PRAGMA foreign_keys=ON")
    ledger = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
    if ledger is None:
        if tables:
            raise SchemaCompatibilityError("database has objects but no recognized migration ledger")
        version = 0
    else:
        try:
            versions = tuple(row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version"))
        except sqlite3.Error:
            raise SchemaCompatibilityError("migration ledger is malformed") from None
        if not versions or versions != tuple(range(1, versions[-1] + 1)):
            raise SchemaCompatibilityError("migration ledger is empty, gapped, or unordered")
        if versions[-1] > CURRENT_SCHEMA_VERSION:
            raise SchemaCompatibilityError("database schema version is newer than this release")
        version = versions[-1]
    preferences = "user_preferences" in tables
    manifest = _expected_manifest(version, preferences)
    validate_schema_manifest(connection, manifest)
    if version == CURRENT_SCHEMA_VERSION:
        _validate_admin_schema(connection)
    integrity, foreign_keys = _database_check(connection)
    if integrity != "ok" or foreign_keys:
        raise SchemaCompatibilityError("database integrity or foreign-key validation failed")
    digest = _data_fingerprint(connection, manifest) if fingerprint_data else ""
    return DatabaseState(version, preferences, hashlib.sha256(repr(manifest).encode()).hexdigest(), digest)


def _validate_ledger(connection: sqlite3.Connection) -> None:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
        raise SchemaCompatibilityError("new migration requires the supported historical ledger")
    columns = tuple(connection.execute("PRAGMA table_info(schema_migrations)"))
    if (tuple(row[1] for row in columns) != ("version", "applied_at")
            or columns[0][2].upper() != "INTEGER" or columns[0][5] != 1
            or columns[1][2].upper() != "TEXT" or columns[1][3] != 1):
        raise SchemaCompatibilityError("migration ledger structure is incompatible")


def _require_database_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        raise SchemaCompatibilityError("database integrity or foreign-key validation failed")


def _database_check(connection: sqlite3.Connection) -> tuple[str, tuple[tuple, ...]]:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check"))
    return integrity, foreign_keys


def _require_no_sidecars(database: Path) -> None:
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise DatabaseInUse("SQLite sidecars must be absent before promotion")


def _require_delete_journal_source(database: Path) -> None:
    _require_no_sidecars(database)
    with database.open("rb") as source:
        header = source.read(20)
    if len(header) < 20 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise DatabaseInUse("promotion requires a rollback-journal SQLite source")
    connection = _connect_readonly(database)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        connection.close()
    if mode != "delete":
        raise DatabaseInUse("promotion requires a checkpointed DELETE-journal source")


def _data_fingerprint(connection: sqlite3.Connection, manifest: SchemaManifest) -> str:
    digest = hashlib.sha256(repr(manifest).encode())
    for table in sorted(manifest.tables + manifest.extension_tables, key=lambda item: item.name):
        columns = tuple(column for column in table.columns if len(column) < 6 or column[5] != 1)
        names = tuple(column[0] for column in columns)
        if not names:
            continue
        primary_key = tuple(column[0] for column in sorted(
            (column for column in columns if column[4]), key=lambda column: column[4]))
        sql_definition = next((obj.sql or "" for obj in manifest.objects + manifest.permitted_extensions
                               if obj.kind == "table" and obj.name == table.name), "")
        if "\x1fWITHOUT\x1fROWID\x1f" in "\x1f" + sql_definition.upper() + "\x1f":
            order = ",".join(_quote_identifier(name) for name in primary_key)
        else:
            aliases = {name.casefold() for name in names}
            rowid = next((name for name in ("_rowid_", "rowid", "oid") if name not in aliases), None)
            order = _quote_identifier(rowid) if rowid else ",".join(_quote_identifier(name) for name in names)
        projection = ",".join(_quote_identifier(name) for name in names)
        digest.update(table.name.encode("utf-8") + b"\0")
        query = f"SELECT {projection} FROM {_quote_identifier(table.name)} ORDER BY {order}"
        for row in connection.execute(query):
            for value in row:
                _update_value_hash(digest, value)
            digest.update(b"\xff")
    sequence = connection.execute("""SELECT name,seq FROM sqlite_sequence ORDER BY name""").fetchall() \
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else ()
    for row in sequence:
        _update_value_hash(digest, row[0])
        _update_value_hash(digest, row[1])
    digest.update(str(connection.execute("PRAGMA user_version").fetchone()[0]).encode())
    digest.update(str(connection.execute("PRAGMA application_id").fetchone()[0]).encode())
    return digest.hexdigest()


def _update_value_hash(digest, value) -> None:
    if value is None:
        tag, payload = b"n", b""
    elif isinstance(value, bytes):
        tag, payload = b"b", value
    elif isinstance(value, str):
        tag, payload = b"s", value.encode("utf-8")
    elif isinstance(value, int):
        tag, payload = b"i", str(value).encode()
    elif isinstance(value, float):
        tag, payload = b"f", struct.pack("!d", value)
    else:
        raise SchemaCompatibilityError("unsupported SQLite value type during fingerprint")
    digest.update(tag + len(payload).to_bytes(8, "big") + payload)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise SchemaCompatibilityError("SQLite database file is unavailable or empty")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _backup_sqlite(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise SafeMigrationError("backup/staging destination already exists")
    src = dst = None
    created = False
    try:
        src = _connect_readonly(source)
        dst = sqlite3.connect(destination, timeout=5, isolation_level=None)
        created = True
        dst.execute("PRAGMA foreign_keys=ON")
        src.backup(dst, pages=128, sleep=0.01)
        dst.close()
        dst = None
        src.close()
        src = None
        os.chmod(destination, 0o600)
        _fsync_file(destination)
    except BaseException as error:
        if dst is not None:
            dst.close()
            dst = None
        if src is not None:
            src.close()
            src = None
        if created:
            destination.unlink(missing_ok=True)
            _remove_sidecars(destination)
        if isinstance(error, SafeMigrationError):
            raise
        if isinstance(error, (sqlite3.Error, OSError)):
            raise SafeMigrationError("SQLite backup/staging copy failed") from None
        raise
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()


def _create_empty_database(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)


def _require_standalone_file(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            raise SafeMigrationError("staging SQLite file still has sidecars")


def _journal_path(database: Path) -> Path:
    return database.with_name(JOURNAL_NAME)


def _artifact_path(directory: Path, name: str | None, kind: str) -> Path | None:
    if name is None:
        return None
    if Path(name).name != name or not name.startswith(f".nexo.sqlite3.{kind}."):
        raise ValueError("migration journal contains an unsafe artifact path")
    path = directory / name
    if path.is_symlink():
        raise ValueError("migration artifact may not be a symlink")
    return path


def _write_journal(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Offline NEXO SQLite migration and recovery tool")
    parser.add_argument("action", choices=("migrate", "recover"))
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--service-stopped", action="store_true",
                        help="operator assertion that NEXO and other database users are stopped")
    args = parser.parse_args()
    if not args.service_stopped:
        parser.error("--service-stopped is required; this tool is offline-only")
    engine = SafeMigrationEngine(args.data_dir)
    result = engine.recover() if args.action == "recover" else engine.migrate()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
