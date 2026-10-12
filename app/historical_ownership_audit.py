"""Narrow audit writer for a future, caller-owned SQLite transaction."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class HistoricalOwnershipAudit:
    operation_id: str
    target_user_id: str
    created_at: float
    conversations_count: int
    notebooks_count: int
    inventory_sha256: str
    assignment_manifest_sha256: str
    backup_id: str
    backup_sha256: str
    sqlite_generation: str

    @property
    def values(self) -> tuple:
        return (self.operation_id, self.target_user_id, self.created_at,
                self.conversations_count, self.notebooks_count, self.inventory_sha256,
                self.assignment_manifest_sha256, self.backup_id, self.backup_sha256,
                self.sqlite_generation, 1, "committed")


def record_historical_ownership_audit(connection: sqlite3.Connection,
                                      audit: HistoricalOwnershipAudit) -> bool:
    """Record audit + terminal operation state; caller must commit/rollback both with domain writes."""
    if not connection.in_transaction:
        raise RuntimeError("audit requires an existing transaction")
    if (not audit.operation_id or not audit.target_user_id or not audit.backup_id
            or not audit.sqlite_generation or min(audit.conversations_count, audit.notebooks_count) < 0
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in
                   (audit.inventory_sha256, audit.assignment_manifest_sha256, audit.backup_sha256))):
        raise ValueError("incomplete historical ownership audit")

    prior = connection.execute("SELECT * FROM auth_historical_ownership_operations WHERE operation_id=?",
                               (audit.operation_id,)).fetchone()
    if prior is not None:
        if tuple(prior) != audit.values:
            raise ValueError("operation id was already used with different parameters")
        event = connection.execute("SELECT 1 FROM auth_security_events WHERE event_type=? AND correlation_id=?",
                                   ("historical_ownership_assigned", audit.operation_id)).fetchone()
        if event is None:
            raise sqlite3.IntegrityError("committed operation is missing its audit event")
        return False

    # The schema trigger creates the matching event in this same transaction.
    connection.execute("""INSERT INTO auth_historical_ownership_operations
        (operation_id,target_user_id,created_at,conversations_count,notebooks_count,inventory_sha256,
         assignment_manifest_sha256,backup_id,backup_sha256,sqlite_generation,audit_contract_version,status)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", audit.values)
    return True
