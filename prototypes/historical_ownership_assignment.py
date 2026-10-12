"""Fixture-only historical ownership assignment prototype; never imported by app.main."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Callable

from app.historical_ownership_audit import HistoricalOwnershipAudit, record_historical_ownership_audit
from app.safe_migrations import (
    CURRENT_SCHEMA_VERSION,
    DatabaseInUse,
    DatabaseLease,
    SafeMigrationError,
    inspect_database,
)
from prototypes.ownership_preparation import SYNTHETIC_APPLICATION_ID
from prototypes.admin_persistence import _temporary_database_path


FIXTURE_CONFIRMATION = "ASSIGN THIS SYNTHETIC FIXTURE ONLY"
_ACTIVE_RUN_STATES = {"started", "running", "waiting_approval"}
_TERMINAL_RUN_STATES = {"completed", "failed"}
_APPROVAL_STATES = {"pending", "approved", "rejected", "executing", "executed",
                    "failed", "expired", "invalidated"}
_INTERACTION_STATES = {"pending", "approved", "denied", "expired"}
_FAULT_HOOK = Callable[[str], None]


class AssignmentRejected(RuntimeError):
    """A fail-closed precondition or operation conflict."""


@dataclass(frozen=True)
class SyntheticAssignmentConsent:
    confirmation: str
    operation_id: str
    target_user_id: str
    conversation_ids: tuple[str, ...]
    notebook_ids: tuple[str, ...]
    manifest: dict
    manifest_sha256: str
    created_at: float


@dataclass(frozen=True)
class AssignmentResult:
    operation_id: str
    target_user_id: str
    conversations_assigned: int
    notebooks_assigned: int
    approvals_invalidated: int
    interactions_denied: int
    already_committed: bool


def assignment_manifest_sha256(manifest: dict) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assign_historical_ownership(database: str | Path, backup: str | Path,
                                consent: SyntheticAssignmentConsent, *,
                                fault_hook: _FAULT_HOOK | None = None) -> AssignmentResult:
    """Assign only explicitly consented fixture rows under a single exclusive SQLite transaction."""
    db_path = _fixture_path(database)
    backup_path = _fixture_path(backup)
    if db_path == backup_path:
        raise AssignmentRejected("source fixture and verified backup must be separate files")
    _validate_consent(consent)
    backup_state, backup_hash, backup_inventory = _verified_backup(backup_path)
    _require_fixture(db_path)

    with _exclusive_fixture_lease(db_path):
        state = _inspect_v31(db_path)
        with sqlite3.connect(db_path, timeout=2, isolation_level=None) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                locked_state = _inspect_v31(db_path)
                if locked_state.schema_digest != state.schema_digest:
                    raise AssignmentRejected("schema changed while acquiring the write transaction")
                if locked_state.data_digest != state.data_digest:
                    raise AssignmentRejected("fixture changed while acquiring the write transaction")

                inventory = _inventory(connection)
                if consent.conversation_ids != tuple(inventory["conversation_ids"]):
                    raise AssignmentRejected("consented conversation inventory does not match the fixture")
                if consent.notebook_ids != tuple(inventory["notebook_ids"]):
                    raise AssignmentRejected("consented notebook inventory does not match the fixture")

                manifest = {
                    "version": 1,
                    "operation_id": consent.operation_id,
                    "target_user_id": consent.target_user_id,
                    "conversation_ids": list(consent.conversation_ids),
                    "notebook_ids": list(consent.notebook_ids),
                }
                if consent.manifest != manifest or assignment_manifest_sha256(manifest) != consent.manifest_sha256:
                    raise AssignmentRejected("synthetic assignment manifest is invalid")

                audit = HistoricalOwnershipAudit(
                    operation_id=consent.operation_id,
                    target_user_id=consent.target_user_id,
                    created_at=consent.created_at,
                    conversations_count=len(consent.conversation_ids),
                    notebooks_count=len(consent.notebook_ids),
                    inventory_sha256=_digest(backup_inventory),
                    assignment_manifest_sha256=consent.manifest_sha256,
                    backup_id=backup_path.name,
                    backup_sha256=backup_hash,
                    sqlite_generation=backup_state.data_digest,
                )

                _validate_identity(connection, consent.target_user_id)
                prior = connection.execute(
                    "SELECT * FROM auth_historical_ownership_operations WHERE operation_id=?",
                    (consent.operation_id,)).fetchone()
                if prior is not None:
                    _validate_committed_retry(connection, prior, audit, consent)
                    inserted = record_historical_ownership_audit(connection, audit)
                    if inserted:
                        raise AssignmentRejected("existing operation unexpectedly changed")
                    connection.commit()
                    return AssignmentResult(consent.operation_id, consent.target_user_id,
                                            len(consent.conversation_ids), len(consent.notebook_ids),
                                            0, 0, True)
                if connection.execute("SELECT 1 FROM auth_historical_ownership_operations LIMIT 1").fetchone():
                    raise AssignmentRejected("a different historical assignment already exists")
                if backup_state.data_digest != state.data_digest:
                    raise AssignmentRejected("verified backup does not match the fixture generation")

                approvals, interactions = _validate_preconditions(connection)
                _fire(fault_hook, "before_ownership")
                conversations = _assign(connection, "conversations", consent.conversation_ids,
                                        consent.target_user_id)
                notebooks = _assign(connection, "notebooks", consent.notebook_ids,
                                    consent.target_user_id)
                _fire(fault_hook, "after_ownership")

                invalidated = 0
                for approval_id in approvals:
                    cursor = connection.execute("""UPDATE pending_approvals SET status='invalidated'
                        WHERE id=? AND status IN ('pending','approved')""", (approval_id,))
                    if cursor.rowcount != 1:
                        raise AssignmentRejected("approval state changed during invalidation")
                    invalidated += 1
                denied = 0
                response = '{"approved":false,"values":{}}'
                completed_at = datetime.fromtimestamp(consent.created_at, timezone.utc).isoformat()
                for interaction_id in interactions:
                    cursor = connection.execute("""UPDATE runtime_interactions
                        SET status='denied',response=?,completed_at=? WHERE id=? AND status='pending'""",
                        (response, completed_at, interaction_id))
                    if cursor.rowcount != 1:
                        raise AssignmentRejected("interaction state changed during invalidation")
                    denied += 1
                _fire(fault_hook, "after_invalidation")

                _verify_assigned_state(connection, consent)
                if record_historical_ownership_audit(connection, audit) is not True:
                    raise AssignmentRejected("operation ID was already committed")
                _fire(fault_hook, "after_audit")
                _fire(fault_hook, "before_commit")
                connection.commit()
                _fire(fault_hook, "after_commit")
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

        _verify_reopened_state(db_path, consent, audit)
    return AssignmentResult(consent.operation_id, consent.target_user_id, conversations, notebooks,
                            invalidated, denied, False)


def _fixture_path(path: str | Path) -> Path:
    try:
        resolved = _temporary_database_path(path)
    except (OSError, ValueError) as error:
        raise AssignmentRejected("assignment accepts only persistent temporary fixture files") from error
    if resolved.name == "nexo.sqlite3":
        raise AssignmentRejected("production database name is forbidden")
    _require_fixture(resolved)
    return resolved


def _inspect_v31(path: Path):
    try:
        state = inspect_database(path)
    except SafeMigrationError as error:
        raise AssignmentRejected("fixture schema, ledger, or integrity validation failed") from error
    if state.version != CURRENT_SCHEMA_VERSION:
        raise AssignmentRejected("fixture must have the exact supported v31 schema")
    return state


@contextmanager
def _exclusive_fixture_lease(database: Path):
    deadline = time.monotonic() + 2.0
    while True:
        lease = DatabaseLease(database, exclusive=True)
        try:
            lease.acquire()
            break
        except DatabaseInUse:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    try:
        yield
    finally:
        lease.close()


def _require_fixture(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise AssignmentRejected("fixture file is unavailable")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA application_id").fetchone()[0] != SYNTHETIC_APPLICATION_ID:
                raise AssignmentRejected("synthetic fixture marker is missing")
    except sqlite3.Error as error:
        raise AssignmentRejected("fixture cannot be inspected") from error


def _verified_backup(path: Path):
    _require_fixture(path)
    source_state = _inspect_v31(path)
    fd, restored_name = tempfile.mkstemp(prefix="nexo-restored-fixture-", suffix=".sqlite3",
                                         dir=path.parent)
    os.close(fd)
    restored = Path(restored_name)
    try:
        _temporary_database_path(restored)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(restored) as target:
            source.backup(target)
        _require_fixture(restored)
        restored_state = _inspect_v31(restored)
        if (restored_state.version != CURRENT_SCHEMA_VERSION
                or restored_state.schema_digest != source_state.schema_digest
                or restored_state.data_digest != source_state.data_digest):
            raise AssignmentRejected("synthetic backup restore verification failed")
        with path.open("rb") as stream:
            backup_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        return source_state, backup_hash, _read_inventory(path)
    finally:
        restored.unlink(missing_ok=True)


def _validate_consent(consent: SyntheticAssignmentConsent) -> None:
    if consent.confirmation != FIXTURE_CONFIRMATION:
        raise AssignmentRejected("explicit synthetic fixture consent is required")
    if (not isinstance(consent.operation_id, str) or not consent.operation_id
            or len(consent.operation_id) > 128
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", consent.operation_id)
            or not isinstance(consent.target_user_id, str) or not consent.target_user_id
            or isinstance(consent.created_at, bool)
            or not isinstance(consent.created_at, (int, float))
            or not math.isfinite(consent.created_at) or not 0 <= consent.created_at <= 253402300799):
        raise AssignmentRejected("consent identity or timestamp is invalid")
    for ids in (consent.conversation_ids, consent.notebook_ids):
        if (not isinstance(ids, tuple) or any(not isinstance(item, str) or not item for item in ids)
                or len(ids) != len(set(ids)) or tuple(sorted(ids)) != ids):
            raise AssignmentRejected("consented resource IDs must be unique non-empty strings")


def _inventory(connection: sqlite3.Connection) -> dict:
    conversations = connection.execute("SELECT id,owner_user_id,notebook_id FROM conversations ORDER BY id").fetchall()
    notebooks = connection.execute("SELECT id,owner_user_id FROM notebooks ORDER BY id").fetchall()
    run_states = _states(connection, "runtime_runs")
    approval_states = _states(connection, "pending_approvals")
    interaction_states = _states(connection, "runtime_interactions")
    operational_tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' "
        "AND (lower(name) LIKE '%queue%' OR lower(name) LIKE '%retry%' OR lower(name) LIKE '%delegat%')")]
    return {
        "conversation_ids": [row["id"] for row in conversations],
        "conversation_owners": [row["owner_user_id"] for row in conversations],
        "notebook_ids": [row["id"] for row in notebooks],
        "notebook_owners": [row["owner_user_id"] for row in notebooks],
        "conversation_notebook_links": [[row["id"], row["notebook_id"]] for row in conversations],
        "run_states": run_states,
        "approval_states": approval_states,
        "interaction_states": interaction_states,
        "operational_tables": operational_tables,
        "relationships": _relationship_counts(connection),
    }


def _read_inventory(path: Path) -> dict:
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return _inventory(connection)


def _validate_identity(connection: sqlite3.Connection, target_user_id: str) -> None:
    state = connection.execute("SELECT state FROM auth_state WHERE id=1").fetchone()
    actors = connection.execute("SELECT actor_user_id FROM auth_security_events WHERE event_type='bootstrap_completed'").fetchall()
    active = connection.execute("SELECT id FROM auth_users WHERE status='active'").fetchall()
    grants = connection.execute("SELECT 1 FROM auth_user_permissions WHERE user_id=? LIMIT 1",
                                (target_user_id,)).fetchone()
    if (not state or state[0] != "bootstrap_completed" or len(actors) != 1
            or actors[0][0] != target_user_id or len(active) != 1
            or active[0][0] != target_user_id or not grants):
        raise AssignmentRejected("unique active bootstrap identity and explicit grant are required in fixture")


def _validate_preconditions(connection: sqlite3.Connection):
    inventory = _inventory(connection)
    if any(owner is not None for owner in inventory["conversation_owners"] + inventory["notebook_owners"]):
        raise AssignmentRejected("pre-existing ownership is ambiguous; refusing reassignment")
    relationships = inventory["relationships"]
    if any(relationships.values()):
        raise AssignmentRejected("critical resource relationships are orphaned or inconsistent")
    if inventory["operational_tables"]:
        raise AssignmentRejected("unknown durable queue, retry, or delegation state requires review")
    _validate_run_states(connection)

    approvals = connection.execute("SELECT id,run_id,status FROM pending_approvals ORDER BY id").fetchall()
    approval_ids = []
    for row in approvals:
        if row["status"] not in _APPROVAL_STATES:
            raise AssignmentRejected("approval state is unknown")
        if row["status"] == "executing":
            raise AssignmentRejected("executing approval requires external effect reconciliation")
        if row["status"] in {"pending", "approved"}:
            run = connection.execute("SELECT status FROM runtime_runs WHERE id=?", (row["run_id"],)).fetchone()
            if not run or run[0] not in _TERMINAL_RUN_STATES:
                raise AssignmentRejected("approval has no proven terminal run")
            approval_ids.append(row["id"])

    interactions = connection.execute("SELECT id,run_id,status FROM runtime_interactions ORDER BY id").fetchall()
    interaction_ids = []
    for row in interactions:
        if row["status"] not in _INTERACTION_STATES:
            raise AssignmentRejected("interaction state is unknown")
        if row["status"] == "pending":
            run = connection.execute("SELECT status FROM runtime_runs WHERE id=?", (row["run_id"],)).fetchone()
            if not run or run[0] not in _TERMINAL_RUN_STATES:
                raise AssignmentRejected("pending interaction has no proven terminal run")
            interaction_ids.append(row["id"])
    return approval_ids, interaction_ids


def _assign(connection: sqlite3.Connection, table: str, ids: tuple[str, ...], target: str) -> int:
    if table not in {"conversations", "notebooks"}:
        raise AssertionError("unapproved ownership table")
    count = 0
    for resource_id in ids:
        cursor = connection.execute(
            f"UPDATE {table} SET owner_user_id=? WHERE id=? AND owner_user_id IS NULL",
            (target, resource_id))
        if cursor.rowcount != 1:
            raise AssignmentRejected("ownership compare-and-set affected an unexpected row count")
        count += cursor.rowcount
    return count


def _verify_assigned_state(connection: sqlite3.Connection, consent: SyntheticAssignmentConsent) -> None:
    for table, ids in (("conversations", consent.conversation_ids), ("notebooks", consent.notebook_ids)):
        actual = tuple(row[0] for row in connection.execute(
            f"SELECT id FROM {table} WHERE owner_user_id=? ORDER BY id", (consent.target_user_id,)))
        if actual != ids:
            raise AssignmentRejected("post-assignment owner set does not match consent")
    if any(_relationship_counts(connection).values()):
        raise AssignmentRejected("post-assignment relationship verification failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise AssignmentRejected("foreign-key verification failed")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise AssignmentRejected("SQLite integrity verification failed")


def _validate_committed_retry(connection: sqlite3.Connection, prior, audit: HistoricalOwnershipAudit,
                              consent: SyntheticAssignmentConsent) -> None:
    if tuple(prior) != audit.values:
        raise AssignmentRejected("operation ID was already used with different consent or backup")
    _verify_assigned_state(connection, consent)
    if connection.execute("SELECT 1 FROM auth_historical_ownership_operations WHERE operation_id<>? LIMIT 1",
                          (consent.operation_id,)).fetchone():
        raise AssignmentRejected("another historical assignment operation exists")
    _validate_no_pending_after_commit(connection)


def _validate_no_pending_after_commit(connection: sqlite3.Connection) -> None:
    _validate_run_states(connection)
    for row in connection.execute("SELECT status FROM pending_approvals"):
        if row[0] not in _APPROVAL_STATES:
            raise AssignmentRejected("committed assignment has an unknown approval state")
    if connection.execute("SELECT 1 FROM pending_approvals WHERE status IN ('pending','approved','executing') LIMIT 1").fetchone():
        raise AssignmentRejected("committed assignment still has an executable approval")
    for row in connection.execute("SELECT status FROM runtime_interactions"):
        if row[0] not in _INTERACTION_STATES:
            raise AssignmentRejected("committed assignment has an unknown interaction state")
    if connection.execute("SELECT 1 FROM runtime_interactions WHERE status='pending' LIMIT 1").fetchone():
        raise AssignmentRejected("committed assignment still has a pending interaction")


def _validate_run_states(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT status,completed_at FROM runtime_runs"):
        status, completed_at = row
        if status not in _ACTIVE_RUN_STATES | _TERMINAL_RUN_STATES:
            raise AssignmentRejected("run state is unknown")
        if status in _ACTIVE_RUN_STATES:
            if completed_at is not None:
                raise AssignmentRejected("active run has contradictory terminal timestamp")
            raise AssignmentRejected("active or recoverable runs require external fencing")
        elif not completed_at:
            raise AssignmentRejected("terminal run is missing its terminal timestamp")


def _verify_reopened_state(database: Path, consent: SyntheticAssignmentConsent,
                           audit: HistoricalOwnershipAudit) -> None:
    _inspect_v31(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        row = connection.execute("SELECT * FROM auth_historical_ownership_operations WHERE operation_id=?",
                                 (consent.operation_id,)).fetchone()
        event = connection.execute("SELECT actor_user_id,created_at FROM auth_security_events "
                                   "WHERE event_type='historical_ownership_assigned' AND correlation_id=?",
                                   (consent.operation_id,)).fetchone()
        if (not row or tuple(row) != audit.values or not event
                or event[0] != consent.target_user_id or event[1] != consent.created_at):
            raise AssignmentRejected("reopened operation/event verification failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise AssignmentRejected("reopened foreign-key verification failed")
        _validate_no_pending_after_commit(connection)


def _states(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[0] for row in connection.execute(f"SELECT status FROM {table} ORDER BY id")]


def _relationship_counts(connection: sqlite3.Connection) -> dict[str, int]:
    queries = {
        "conversation_notebook_orphans": """SELECT COUNT(*) FROM conversations c LEFT JOIN notebooks n ON n.id=c.notebook_id WHERE c.notebook_id IS NOT NULL AND n.id IS NULL""",
        "chunk_ancestry": """SELECT COUNT(*) FROM document_chunks dc LEFT JOIN canonical_documents d ON d.id=dc.document_id LEFT JOIN notebook_sources s ON s.id=dc.source_id WHERE d.id IS NULL OR s.id IS NULL OR dc.notebook_id!=d.notebook_id OR dc.notebook_id!=s.notebook_id OR d.source_id!=s.id""",
        "citation_orphans": """SELECT COUNT(*) FROM message_citations c LEFT JOIN messages m ON m.id=c.message_id LEFT JOIN notebooks n ON n.id=c.notebook_id LEFT JOIN notebook_sources s ON s.id=c.source_id LEFT JOIN canonical_documents d ON d.id=c.document_id LEFT JOIN document_chunks k ON k.id=c.chunk_id WHERE m.id IS NULL OR n.id IS NULL OR s.id IS NULL OR d.id IS NULL OR k.id IS NULL""",
        "citation_ancestry": """SELECT COUNT(*) FROM message_citations c JOIN notebook_sources s ON s.id=c.source_id JOIN canonical_documents d ON d.id=c.document_id JOIN document_chunks k ON k.id=c.chunk_id WHERE s.notebook_id!=c.notebook_id OR d.notebook_id!=c.notebook_id OR k.notebook_id!=c.notebook_id OR d.source_id!=c.source_id OR k.source_id!=c.source_id OR k.document_id!=c.document_id""",
        "run_message_orphans": """SELECT COUNT(*) FROM runtime_runs r LEFT JOIN messages m ON m.id=r.message_id WHERE m.id IS NULL OR m.conversation_id!=r.conversation_id""",
        "interaction_lineage": """SELECT COUNT(*) FROM runtime_interactions i JOIN runtime_runs r ON r.id=i.run_id WHERE i.conversation_id!=r.conversation_id""",
        "approval_lineage": """SELECT COUNT(*) FROM pending_approvals a JOIN runtime_runs r ON r.id=a.run_id LEFT JOIN messages m ON m.id=a.message_id WHERE a.conversation_id!=r.conversation_id OR m.id IS NULL OR m.conversation_id!=a.conversation_id""",
        "trace_orphans": """SELECT COUNT(*) FROM runtime_trace_events t LEFT JOIN conversations c ON c.id=t.conversation_id LEFT JOIN messages m ON m.id=t.message_id WHERE c.id IS NULL OR m.id IS NULL OR m.conversation_id!=t.conversation_id""",
        "shadow_orphans": """SELECT COUNT(*) FROM shadow_observations s LEFT JOIN conversations c ON c.id=s.conversation_id LEFT JOIN messages m ON m.id=s.message_id WHERE c.id IS NULL OR m.id IS NULL OR m.conversation_id!=s.conversation_id""",
        "global_profile_notebook_links": """SELECT COUNT(*) FROM agent_profile_notebooks apn JOIN notebooks n ON n.id=apn.notebook_id WHERE n.owner_user_id IS NULL""",
    }
    return {name: connection.execute(sql).fetchone()[0] for name, sql in queries.items()}


def _digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fire(hook: _FAULT_HOOK | None, stage: str) -> None:
    if hook:
        hook(stage)
