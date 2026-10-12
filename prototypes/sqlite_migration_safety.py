"""Experimental SQLite migration checks; never imported by the NEXO runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import sqlite3


class MigrationStateError(RuntimeError):
    pass


class MigrationValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseCheck:
    integrity: str
    foreign_key_violations: tuple[tuple, ...]
    migration_versions: tuple[int, ...]


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


SchemaValidator = Callable[[sqlite3.Connection], None]
FaultHook = Callable[[str, sqlite3.Connection], None]


def check_database(connection: sqlite3.Connection) -> DatabaseCheck:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check"))
    exists = connection.execute("""SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='schema_migrations'""").fetchone()
    versions = tuple(row[0] for row in connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version")) if exists else ()
    return DatabaseCheck(integrity, foreign_keys, versions)


def capture_schema_manifest(connection: sqlite3.Connection,
                            permitted_additional: Iterable[tuple[str, str]] = ()) -> SchemaManifest:
    """Capture a trusted fixture/build schema; never bless an untrusted runtime DB this way."""
    permitted = frozenset(permitted_additional)
    objects = tuple(SchemaObject(row[0], row[1], row[2], row[3])
                    for row in connection.execute("""SELECT type,name,tbl_name,sql FROM sqlite_master
                        WHERE type IN ('table','index','view','trigger') AND name NOT GLOB 'sqlite_*'
                        ORDER BY type,name"""))
    object_ids = {(item.kind, item.name) for item in objects}
    if not permitted.issubset(object_ids):
        raise ValueError("permitted schema extensions must exist in the trusted fixture")
    expected_objects = tuple(item for item in objects if (item.kind, item.name) not in permitted)
    extension_objects = tuple(item for item in objects if (item.kind, item.name) in permitted)
    table_names = {item.name for item in objects if item.kind == "table"}
    expected_names = {item.name for item in expected_objects if item.kind == "table"}
    extension_names = {item.name for item in extension_objects if item.kind == "table"}
    structures = {name: _table_structure(connection, name) for name in table_names}
    return SchemaManifest(expected_objects, extension_objects,
                          tuple(structures[name] for name in sorted(expected_names)),
                          tuple(structures[name] for name in sorted(extension_names)))


def validate_schema_manifest(connection: sqlite3.Connection, expected: SchemaManifest) -> None:
    """Reject missing, changed, and unlisted objects; extensions need full explicit definitions."""
    permitted = tuple((item.kind, item.name) for item in expected.permitted_extensions)
    try:
        actual = capture_schema_manifest(connection, permitted)
    except ValueError:
        raise MigrationValidationError("SQLite schema objects do not match manifest") from None
    if actual != expected:
        raise MigrationValidationError("SQLite schema objects do not match manifest")


def apply_transactional_migration(connection: sqlite3.Connection, version: int,
                                  statements: Iterable[str], validate_previous: SchemaValidator,
                                  validate_schema: SchemaValidator,
                                  fault: FaultHook | None = None) -> bool:
    """Apply one single-statement-per-entry migration under explicit BEGIN IMMEDIATE."""
    if type(version) is not int or version < 1 or connection.in_transaction:
        raise ValueError("migration requires a positive version and an idle connection")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise MigrationStateError("foreign-key enforcement must be enabled before migration")

    def inject(stage: str) -> None:
        if fault is not None:
            fault(stage, connection)

    try:
        connection.execute("BEGIN IMMEDIATE")
        ledger = connection.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='schema_migrations'""").fetchone()
        if ledger is None:
            connection.execute("""CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""")
        else:
            ledger_columns = tuple(connection.execute("PRAGMA table_info(schema_migrations)"))
            columns = tuple(row[1] for row in ledger_columns)
            if (columns != ("version", "applied_at") or ledger_columns[0][2].upper() != "INTEGER"
                    or ledger_columns[0][5] != 1 or ledger_columns[1][2].upper() != "TEXT"
                    or ledger_columns[1][3] != 1):
                raise MigrationStateError("migration ledger shape is unknown")

        versions = tuple(row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"))
        if versions == tuple(range(1, version + 1)):
            validate_schema(connection)
            _require_valid_database(connection)
            connection.commit()
            return False
        if versions != tuple(range(1, version)):
            raise MigrationStateError("migration ledger has a gap or unknown version")

        validate_previous(connection)
        _require_valid_database(connection)
        inject("before_migration")
        for index, statement in enumerate(statements, start=1):
            connection.execute(statement)
            inject(f"after_statement:{index}")
        validate_schema(connection)
        _require_valid_database(connection)
        inject("before_ledger")
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, datetime('now'))",
                           (version,))
        inject("after_ledger")
        connection.commit()
        return True
    except BaseException:
        connection.rollback()
        raise


def _require_valid_database(connection: sqlite3.Connection) -> None:
    check = check_database(connection)
    if check.integrity != "ok" or check.foreign_key_violations:
        raise MigrationValidationError("SQLite integrity or foreign-key validation failed")


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
