"""Synthetic-only first-admin/ownership transition contract simulator."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from app.historical_ownership_audit import HistoricalOwnershipAudit, record_historical_ownership_audit
from app.safe_migrations import (
    CURRENT_SCHEMA_VERSION,
    DatabaseLease,
    _inspect_connection,
    inspect_database,
)
from prototypes.historical_ownership_assignment import _relationship_counts
from prototypes.admin_auth import ADMIN_PERMISSIONS
from prototypes.ownership_preparation import SYNTHETIC_APPLICATION_ID
from prototypes.admin_persistence import _temporary_database_path


FIXTURE_CONFIRMATION = "COMMIT THIS SYNTHETIC BOOTSTRAP TRANSITION"
REQUIRED_ADMIN_PERMISSIONS = frozenset({"config:read", "config:write", "chat:read", "chat:write"})
APPROVAL_STATES = frozenset({"pending", "approved", "rejected", "executing", "executed",
                             "failed", "expired", "invalidated"})
INTERACTION_STATES = frozenset({"pending", "approved", "denied", "expired"})


class TransitionRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionFence:
    ingress_closed: bool
    process_inventory_complete: bool
    all_nexo_processes_stopped: bool
    external_fencing_confirmed: bool
    no_effects_in_flight: bool
    lease_expires_at: float
    now: float


@dataclass(frozen=True)
class TransitionConsent:
    confirmation: str
    operation_id: str
    database_generation: str
    target_user_id: str
    username: str
    password_hash: str
    grants: frozenset[str]
    backup_id: str
    backup_sha256: str
    manifest: dict
    manifest_sha256: str
    created_at: float


def canonical_sha256(value: dict) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def commit_synthetic_transition(database: str | Path, backup: str | Path,
                                consent: TransitionConsent, fence: ExecutionFence) -> bool:
    """Commit first identity, grants, ownership, invalidation, audit and bootstrap atomically."""
    if not isinstance(consent, TransitionConsent) or not isinstance(fence, ExecutionFence):
        raise TransitionRejected("explicit transition consent and execution fence are required")
    db_path = _fixture(database)
    backup_path = _fixture(backup)
    if db_path == backup_path:
        raise TransitionRejected("source and backup must be different fixtures")
    if consent.confirmation != FIXTURE_CONFIRMATION:
        raise TransitionRejected("explicit fixture consent required")
    if (not consent.operation_id or not consent.target_user_id or not consent.username.strip()
            or not consent.password_hash or not consent.backup_id
            or not _sha256(consent.backup_sha256) or not _sha256(consent.manifest_sha256)
            or not math.isfinite(consent.created_at)):
        raise TransitionRejected("invalid transition identity or digest")
    if not (fence.ingress_closed and fence.process_inventory_complete
            and fence.all_nexo_processes_stopped and fence.external_fencing_confirmed
            and fence.no_effects_in_flight and fence.lease_expires_at > fence.now):
        raise TransitionRejected("execution fence is incomplete, expired, or unverified")

    with DatabaseLease(db_path, exclusive=True):
        before = inspect_database(db_path)
        backup_state = inspect_database(backup_path)
        if before.version != CURRENT_SCHEMA_VERSION or backup_state.version != CURRENT_SCHEMA_VERSION:
            raise TransitionRejected("schema or source/backup generation mismatch")
        if backup_state.data_digest != consent.database_generation:
            raise TransitionRejected("backup generation does not match consent")
        with backup_path.open("rb") as backup_file:
            backup_sha = hashlib.file_digest(backup_file, "sha256").hexdigest()
        if backup_sha != consent.backup_sha256 or backup_path.name != consent.backup_id:
            raise TransitionRejected("backup does not match consent")

        with sqlite3.connect(db_path, timeout=1, isolation_level=None) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                locked = _inspect_connection(connection, fingerprint_data=True)
                state = connection.execute("SELECT state FROM auth_state WHERE id=1").fetchone()
                prior = connection.execute(
                    "SELECT * FROM auth_historical_ownership_operations WHERE operation_id=?",
                    (consent.operation_id,)).fetchone()
                if state == ("bootstrap_completed",) and prior is not None:
                    _verify_committed_retry(connection, prior, consent, backup_path, backup_sha)
                    connection.rollback()
                    return False
                if (locked.schema_digest != before.schema_digest
                        or locked.data_digest != before.data_digest
                        or locked.data_digest != consent.database_generation
                        or backup_state.data_digest != locked.data_digest):
                    raise TransitionRejected("inventory changed or backup is stale")
                if state != ("schema_installed",):
                    raise TransitionRejected("bootstrap state is not pristine")
                if any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                       for table in ("auth_users", "auth_user_permissions", "auth_sessions", "auth_security_events")):
                    raise TransitionRejected("administrative identity state is not empty")
                if any(_relationship_counts(connection).values()):
                    raise TransitionRejected("ambiguous resource relationships require manual review")

                conversations = tuple(row[0] for row in connection.execute(
                    "SELECT id FROM conversations ORDER BY id"))
                notebooks = tuple(row[0] for row in connection.execute(
                    "SELECT id FROM notebooks ORDER BY id"))
                approvals = tuple(connection.execute(
                    "SELECT id,status FROM pending_approvals WHERE status IN ('pending','approved') ORDER BY id"))
                interactions = tuple(connection.execute(
                    "SELECT id,status FROM runtime_interactions WHERE status='pending' ORDER BY id"))
                active_runs = connection.execute(
                    "SELECT 1 FROM runtime_runs WHERE status NOT IN ('completed','failed') "
                    "OR completed_at IS NULL LIMIT 1").fetchone()
                unknown_approval = connection.execute(
                    "SELECT 1 FROM pending_approvals WHERE status NOT IN "
                    "('pending','approved','rejected','executing','executed','failed','expired','invalidated') LIMIT 1").fetchone()
                unknown_interaction = connection.execute(
                    "SELECT 1 FROM runtime_interactions WHERE status NOT IN "
                    "('pending','approved','denied','expired') LIMIT 1").fetchone()
                if (active_runs or unknown_approval or unknown_interaction or connection.execute(
                        "SELECT 1 FROM pending_approvals WHERE status='executing' LIMIT 1").fetchone()):
                    raise TransitionRejected("active or ambiguous execution state blocks transition")
                if any(owner is not None for owner, in connection.execute(
                        "SELECT owner_user_id FROM conversations UNION ALL SELECT owner_user_id FROM notebooks")):
                    raise TransitionRejected("existing ownership is ambiguous")

                expected = {
                    "version": 1, "operation_id": consent.operation_id,
                    "database_generation": before.data_digest, "target_user_id": consent.target_user_id,
                    "backup_id": backup_path.name, "backup_sha256": backup_sha,
                    "conversation_ids": list(conversations), "notebook_ids": list(notebooks),
                    "conversation_count": len(conversations), "notebook_count": len(notebooks),
                    "approval_ids_to_invalidate": [row[0] for row in approvals],
                    "interaction_ids_to_deny": [row[0] for row in interactions],
                    "relationships": _relationship_snapshot(connection),
                }
                if consent.manifest != expected or canonical_sha256(expected) != consent.manifest_sha256:
                    raise TransitionRejected("consent does not bind the current inventory")
                if (not REQUIRED_ADMIN_PERMISSIONS.issubset(consent.grants)
                        or not consent.grants.issubset(ADMIN_PERMISSIONS)):
                    raise TransitionRejected("explicit administrative grants are insufficient")

                connection.execute("INSERT INTO auth_users VALUES(?,?,?,?,?,?,?)", (
                    consent.target_user_id, consent.username, consent.username.casefold(),
                    consent.password_hash, "active", consent.created_at, consent.created_at))
                connection.executemany("INSERT INTO auth_user_permissions(user_id,permission) VALUES(?,?)",
                                       ((consent.target_user_id, item) for item in sorted(consent.grants)))
                actual_grants = frozenset(row[0] for row in connection.execute(
                    "SELECT permission FROM auth_user_permissions WHERE user_id=?", (consent.target_user_id,)))
                if actual_grants != consent.grants:
                    raise TransitionRejected("authority verification failed")

                for table, ids in (("conversations", conversations), ("notebooks", notebooks)):
                    for resource_id in ids:
                        changed = connection.execute(
                            f"UPDATE {table} SET owner_user_id=? WHERE id=? AND owner_user_id IS NULL",
                            (consent.target_user_id, resource_id)).rowcount
                        if changed != 1:
                            raise TransitionRejected("stale ownership inventory")
                connection.execute("UPDATE pending_approvals SET status='invalidated' "
                                   "WHERE status IN ('pending','approved')")
                connection.execute("UPDATE runtime_interactions SET status='denied',"
                                   "response='{" + '"approved":false,"values":{}' + "}' "
                                   "WHERE status='pending'")

                audit = HistoricalOwnershipAudit(
                    consent.operation_id, consent.target_user_id, consent.created_at,
                    len(conversations), len(notebooks), before.data_digest,
                    consent.manifest_sha256, backup_path.name, backup_sha,
                    before.data_digest)
                record_historical_ownership_audit(connection, audit)
                connection.execute("INSERT INTO auth_security_events"
                                   "(event_type,actor_user_id,created_at,correlation_id)"
                                   " VALUES('bootstrap_completed',?,?,?)",
                                   (consent.target_user_id, consent.created_at, consent.operation_id))
                connection.execute("UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at=? "
                                   "WHERE id=1 AND state='schema_installed'", (str(consent.created_at),))
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise TransitionRejected("bootstrap completion compare-and-set failed")
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise TransitionRejected("foreign-key verification failed")
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    inspect_database(db_path)
    return True


def _fixture(path: str | Path) -> Path:
    try:
        resolved = _temporary_database_path(path)
        with sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA application_id").fetchone()[0] != SYNTHETIC_APPLICATION_ID:
                raise TransitionRejected("synthetic fixture marker is missing")
        return resolved
    except (OSError, ValueError, sqlite3.Error) as error:
        raise TransitionRejected("only marked temporary synthetic SQLite files are accepted") from error


def _verify_committed_retry(connection: sqlite3.Connection, prior: tuple,
                            consent: TransitionConsent, backup_path: Path,
                            backup_sha: str) -> None:
    manifest = consent.manifest
    conversations = tuple(manifest.get("conversation_ids", ()))
    notebooks = tuple(manifest.get("notebook_ids", ()))
    approvals = tuple(manifest.get("approval_ids_to_invalidate", ()))
    interactions = tuple(manifest.get("interaction_ids_to_deny", ()))
    if (tuple(sorted(set(conversations))) != conversations
            or tuple(sorted(set(notebooks))) != notebooks
            or tuple(sorted(set(approvals))) != approvals
            or tuple(sorted(set(interactions))) != interactions
            or manifest.get("operation_id") != consent.operation_id
            or manifest.get("database_generation") != consent.database_generation
            or manifest.get("target_user_id") != consent.target_user_id
            or manifest.get("backup_id") != backup_path.name
            or manifest.get("backup_sha256") != backup_sha
            or manifest.get("conversation_count") != len(conversations)
            or manifest.get("notebook_count") != len(notebooks)
            or manifest.get("approval_ids_to_invalidate") != list(approvals)
            or manifest.get("interaction_ids_to_deny") != list(interactions)
            or manifest.get("relationships") != _relationship_snapshot(connection)
            or canonical_sha256(manifest) != consent.manifest_sha256):
        raise TransitionRejected("retry manifest is inconsistent")

    expected_audit = HistoricalOwnershipAudit(
        consent.operation_id, consent.target_user_id, consent.created_at,
        len(conversations), len(notebooks), consent.database_generation,
        consent.manifest_sha256, backup_path.name, backup_sha,
        consent.database_generation,
    )
    if tuple(prior) != expected_audit.values:
        raise TransitionRejected("retry parameters contradict the committed operation")
    if connection.execute("SELECT state,bootstrapped_at FROM auth_state WHERE id=1").fetchone() != (
            "bootstrap_completed", str(consent.created_at)):
        raise TransitionRejected("committed bootstrap state is inconsistent")

    user = connection.execute("SELECT username,password_hash,status FROM auth_users WHERE id=?",
                              (consent.target_user_id,)).fetchone()
    if user != (consent.username, consent.password_hash, "active"):
        raise TransitionRejected("committed administrator identity is inconsistent")
    all_users = tuple(connection.execute("SELECT id,status FROM auth_users ORDER BY id"))
    if all_users != ((consent.target_user_id, "active"),) or connection.execute(
            "SELECT COUNT(*) FROM auth_sessions").fetchone()[0] != 0:
        raise TransitionRejected("committed administrative state contains unexpected identities or sessions")
    grants = frozenset(row[0] for row in connection.execute(
        "SELECT permission FROM auth_user_permissions WHERE user_id=?", (consent.target_user_id,)))
    all_grants = tuple(connection.execute("SELECT user_id,permission FROM auth_user_permissions "
                                          "ORDER BY user_id,permission"))
    expected_grants = tuple((consent.target_user_id, item) for item in sorted(consent.grants))
    if (all_grants != expected_grants or grants != consent.grants
            or not REQUIRED_ADMIN_PERMISSIONS.issubset(grants)
            or not grants.issubset(ADMIN_PERMISSIONS)):
        raise TransitionRejected("committed administrator authority is inconsistent")

    for table, expected_ids in (("conversations", conversations), ("notebooks", notebooks)):
        rows = tuple(connection.execute(f"SELECT id,owner_user_id FROM {table} ORDER BY id"))
        if tuple(row[0] for row in rows) != expected_ids or any(
                row[1] != consent.target_user_id for row in rows):
            raise TransitionRejected("committed ownership does not match the consented inventory")
    if (manifest.get("conversation_count") != prior[3]
            or manifest.get("notebook_count") != prior[4]
            or any(_relationship_counts(connection).values())):
        raise TransitionRejected("committed resource counts or relationships are inconsistent")

    for table, expected_ids, expected_status in (
            ("pending_approvals", approvals, "invalidated"),
            ("runtime_interactions", interactions, "denied")):
        states = {row[0] for row in connection.execute(f"SELECT DISTINCT status FROM {table}")}
        allowed_states = APPROVAL_STATES if table == "pending_approvals" else INTERACTION_STATES
        if not states.issubset(allowed_states):
            raise TransitionRejected("committed transition contains an unknown capability state")
        current = tuple(row[0] for row in connection.execute(f"SELECT id FROM {table} WHERE status=? ORDER BY id",
                                                              (expected_status,)))
        if current != expected_ids:
            raise TransitionRejected("committed invalidation state does not match consent")
        if table == "pending_approvals":
            executable = connection.execute(
                "SELECT 1 FROM pending_approvals WHERE status IN ('pending','approved','executing') LIMIT 1").fetchone()
        else:
            executable = connection.execute(
                "SELECT 1 FROM runtime_interactions WHERE status='pending' LIMIT 1").fetchone()
        if executable:
            raise TransitionRejected("committed state still contains an executable capability")

    if connection.execute("SELECT 1 FROM runtime_runs WHERE status NOT IN ('completed','failed') "
                          "OR completed_at IS NULL LIMIT 1").fetchone():
        raise TransitionRejected("committed transition contains an active or unknown run")

    history = tuple(connection.execute("SELECT actor_user_id,created_at,correlation_id FROM auth_security_events "
                                       "WHERE event_type='historical_ownership_assigned' AND correlation_id=?",
                                       (consent.operation_id,)))
    bootstrap = tuple(connection.execute("SELECT actor_user_id,created_at,correlation_id FROM auth_security_events "
                                         "WHERE event_type='bootstrap_completed' AND correlation_id=?",
                                         (consent.operation_id,)))
    if (history != ((consent.target_user_id, consent.created_at, consent.operation_id),)
            or bootstrap != ((consent.target_user_id, consent.created_at, consent.operation_id),)
            or connection.execute("SELECT COUNT(*) FROM auth_historical_ownership_operations").fetchone()[0] != 1
            or connection.execute("PRAGMA foreign_key_check").fetchone()
            or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
        raise TransitionRejected("committed transition audit or integrity is inconsistent")


def _relationship_snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    queries = {
        "conversation_notebook": "SELECT id,notebook_id FROM conversations ORDER BY id",
        "message_conversation": "SELECT id,conversation_id FROM messages ORDER BY id",
        "run_lineage": "SELECT id,conversation_id,message_id FROM runtime_runs ORDER BY id",
        "runtime_event_run": "SELECT id,run_id FROM runtime_events ORDER BY id",
        "interaction_lineage": "SELECT id,run_id,conversation_id FROM runtime_interactions ORDER BY id",
        "approval_lineage": "SELECT id,run_id,conversation_id,message_id FROM pending_approvals ORDER BY id",
        "notebook_source": "SELECT id,notebook_id FROM notebook_sources ORDER BY id",
        "document_lineage": "SELECT id,notebook_id,source_id FROM canonical_documents ORDER BY id",
        "span_document": "SELECT id,document_id FROM canonical_spans ORDER BY id",
        "chunk_lineage": "SELECT id,notebook_id,document_id,source_id FROM document_chunks ORDER BY id",
        "citation_lineage": "SELECT id,message_id,notebook_id,source_id,document_id,chunk_id FROM message_citations ORDER BY id",
        "trace_lineage": "SELECT id,conversation_id,message_id FROM runtime_trace_events ORDER BY id",
        "shadow_lineage": "SELECT id,conversation_id,message_id FROM shadow_observations ORDER BY id",
        "profile_notebook": "SELECT agent_profile_id,notebook_id FROM agent_profile_notebooks ORDER BY agent_profile_id,notebook_id",
    }
    return {name: list(connection.execute(query)) for name, query in queries.items()}


def _sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)
