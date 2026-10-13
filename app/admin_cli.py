"""Read-only administrative inspection for synthetic NEXO SQLite fixtures."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile

from app import safe_migrations


SYNTHETIC_APPLICATION_ID = 0x4E584F31
SUPPORTED_VERSIONS = (28, 29, 30, 31)


class CliError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


def _fixture_path(value: str) -> Path:
    if os.geteuid() == 0:
        raise CliError(2, "root execution is not permitted")
    supplied = Path(value)
    if not supplied.is_absolute() or ".." in supplied.parts:
        raise CliError(2, "path must be canonical and absolute")

    temp_root = Path(tempfile.gettempdir()).absolute()
    try:
        relative = supplied.relative_to(temp_root)
    except ValueError:
        raise CliError(2, "path is outside the synthetic fixture root") from None
    if not relative.parts or supplied.name == "nexo.sqlite3":
        raise CliError(2, "path is not an authorized synthetic fixture")

    candidate = temp_root / relative
    current = temp_root
    for part in relative.parts:
        current /= part
        try:
            details = current.lstat()
        except OSError:
            raise CliError(3, "path is unavailable") from None
        if stat.S_ISLNK(details.st_mode):
            raise CliError(2, "symlink paths are not permitted")

    try:
        resolved_root = temp_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
        details = candidate.stat()
        parent = candidate.parent.stat()
    except (OSError, ValueError):
        raise CliError(3, "path is unavailable") from None
    if not stat.S_ISREG(details.st_mode) or details.st_size == 0:
        raise CliError(2, "database must be a non-empty regular file")
    if not details.st_mode & stat.S_IRUSR or not os.access(candidate, os.R_OK):
        raise CliError(3, "fixture is not readable by the current user")
    if details.st_uid != os.geteuid() or parent.st_uid != os.geteuid():
        raise CliError(2, "fixture ownership does not match the current user")
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise CliError(2, "group/other-writable fixture paths are not permitted")

    configured_data = Path(os.getenv("NEXO_DATA_DIR", "./data")).absolute() / "nexo.sqlite3"
    try:
        candidate.resolve().relative_to(configured_data.parent.resolve())
    except (OSError, ValueError):
        pass
    else:
        raise CliError(2, "configured NEXO data paths are not permitted")
    return candidate


def _sidecars(path: Path) -> tuple[Path, ...]:
    found = []
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        found.append(sidecar)
    return tuple(found)


def _inspect(path: Path):
    if _sidecars(path):
        raise CliError(4, "SQLite sidecars are present; refusing inspection")
    try:
        with path.open("rb") as source:
            header = source.read(20)
    except OSError:
        raise CliError(3, "fixture is not readable by the current user") from None
    if len(header) < 20 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise CliError(4, "unsupported or invalid SQLite file header")
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0.5)
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        if application_id != SYNTHETIC_APPLICATION_ID:
            raise CliError(2, "database is not marked as a synthetic fixture")
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
            raise CliError(4, "unsupported SQLite journal mode")
        state = safe_migrations._inspect_connection(connection, fingerprint_data=True)
        if state.version not in SUPPORTED_VERSIONS:
            raise CliError(4, "schema version is not supported by this CLI")
        return state, connection
    except CliError:
        if connection is not None:
            connection.close()
        raise
    except (sqlite3.Error, safe_migrations.SafeMigrationError, OSError):
        if connection is not None:
            connection.close()
        raise CliError(4, "SQLite fixture is corrupt or incompatible") from None


def _emit(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True))


def _base_payload(path: Path, state) -> dict:
    return {
        "fixture": True,
        "file": path.name,
        "schema_version": state.version,
        "schema_digest": state.schema_digest,
        "generation": state.data_digest,
        "read_only": True,
    }


def _inspect_command(args) -> int:
    path = _fixture_path(args.database)
    state, connection = _inspect(path)
    connection.close()
    result = _base_payload(path, state) | {"ledger": "contiguous", "integrity": "ok", "foreign_keys": "ok"}
    if args.expected_generation and state.data_digest != args.expected_generation:
        raise CliError(4, "database generation does not match the expected generation")
    _emit(result)
    return 0


def _status_command(args) -> int:
    path = _fixture_path(args.database)
    state, connection = _inspect(path)
    try:
        row = connection.execute("SELECT state FROM auth_state WHERE id=1").fetchone() \
            if state.version >= 29 else None
    finally:
        connection.close()
    admin_state = row[0] if row else "not_installed"
    blockers = []
    if admin_state != "bootstrap_completed":
        blockers.append("administrative_bootstrap_incomplete")
    blockers.extend(("http_authentication_not_integrated", "operational_readiness_not_assessed"))
    _emit(_base_payload(path, state) | {
        "administrative_state": admin_state,
        "blockers": blockers,
        "operational_readiness": False,
    })
    return 0


def _check_path_command(args) -> int:
    path = _fixture_path(args.database)
    state, connection = _inspect(path)
    connection.close()
    _emit({"valid": True, "fixture": True, "file": path.name,
           "schema_version": state.version, "read_only": True,
           "race_free_open_guarantee": False})
    return 0


def _backup_verify_command(args) -> int:
    target_path = _fixture_path(args.database)
    backup_path = _fixture_path(args.backup)
    target, target_connection = _inspect(target_path)
    target_connection.close()
    backup, backup_connection = _inspect(backup_path)
    backup_connection.close()
    if (target.version, target.schema_digest, target.data_digest) != \
            (backup.version, backup.schema_digest, backup.data_digest):
        raise CliError(4, "backup does not match the target fixture generation")
    _emit({"valid": True, "schema_version": backup.version,
           "generation_matches_target": True, "integrity": "ok",
           "foreign_keys": "ok", "restore_tested": False, "read_only": True})
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nexo-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--database", required=True)
    inspect.add_argument("--expected-generation")
    inspect.set_defaults(run=_inspect_command)
    status = commands.add_parser("status")
    status.add_argument("--database", required=True)
    status.set_defaults(run=_status_command)
    check_path = commands.add_parser("check-path")
    check_path.add_argument("--database", required=True)
    check_path.set_defaults(run=_check_path_command)
    backup = commands.add_parser("backup-verify")
    backup.add_argument("--database", required=True)
    backup.add_argument("--backup", required=True)
    backup.set_defaults(run=_backup_verify_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.run(args)
    except CliError as error:
        print(json.dumps({"error": error.message, "code": error.code}, sort_keys=True), file=sys.stderr)
        return error.code


if __name__ == "__main__":
    raise SystemExit(main())
