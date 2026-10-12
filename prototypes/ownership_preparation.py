"""Read-only ownership preparation report for temporary synthetic SQLite files."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from app.safe_migrations import ADMIN_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION, OWNERSHIP_SCHEMA_VERSION, inspect_database
from prototypes.admin_persistence import _temporary_database_path


_RESOURCE_TABLES = (
    "conversations", "messages", "runtime_runs", "runtime_events", "runtime_trace_events",
    "shadow_observations", "runtime_interactions", "pending_approvals", "conversation_tool_approvals",
    "notebooks", "notebook_sources", "canonical_documents", "canonical_spans", "document_chunks",
    "message_citations",
)
_GLOBAL_TABLES = (
    "providers", "models", "agent_profiles", "agent_profile_tools", "agent_profile_notebooks",
    "embedding_configurations", "mcp_servers", "mcp_tools", "aemet_credentials", "user_preferences",
)
SYNTHETIC_APPLICATION_ID = 0x4E584F54


def _require_synthetic_fixture(database: Path) -> None:
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA application_id").fetchone()[0] != SYNTHETIC_APPLICATION_ID:
                raise ValueError("ownership preparation requires an explicitly marked synthetic fixture")
        finally:
            connection.close()
    except sqlite3.Error:
        raise ValueError("ownership preparation requires an existing readable SQLite fixture") from None


def inspect_ownership_preparation(path: str | Path) -> dict:
    """Report structural blockers without writing or exposing row contents/identifiers."""
    database = _temporary_database_path(path)
    _require_synthetic_fixture(database)
    state = inspect_database(database, fingerprint_data=False)
    if state.version not in (ADMIN_SCHEMA_VERSION, OWNERSHIP_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION):
        raise ValueError("ownership preparation requires schema v29, v30, or v31")

    uri = database.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        version = state.version

        def count(sql: str) -> int:
            return connection.execute(sql).fetchone()[0]

        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        resources = {table: count(f"SELECT COUNT(*) FROM {table}") for table in _RESOURCE_TABLES}
        global_resources = {table: count(f"SELECT COUNT(*) FROM {table}")
                            for table in _GLOBAL_TABLES if table in tables}
        if version == CURRENT_SCHEMA_VERSION:
            ownership = {
                "conversations": {"supported": True, "total": resources["conversations"],
                                  "unowned": count("SELECT COUNT(*) FROM conversations WHERE owner_user_id IS NULL")},
                "notebooks": {"supported": True, "total": resources["notebooks"],
                              "unowned": count("SELECT COUNT(*) FROM notebooks WHERE owner_user_id IS NULL")},
            }
        else:
            ownership = {
                table: {"supported": False, "total": resources[table], "unowned": resources[table]}
                for table in ("conversations", "notebooks")
            }

        admin_state = connection.execute(
            "SELECT state FROM auth_state WHERE id=1").fetchone()[0]
        active_users = count("SELECT COUNT(*) FROM auth_users WHERE status='active'")
        bootstrap_actors = count("SELECT COUNT(*) FROM auth_security_events "
                                 "WHERE event_type='bootstrap_completed'")
        approvals = {row[0]: row[1] for row in connection.execute(
            "SELECT status,COUNT(*) FROM pending_approvals GROUP BY status ORDER BY status")}
        interactions = {row[0]: row[1] for row in connection.execute(
            "SELECT status,COUNT(*) FROM runtime_interactions GROUP BY status ORDER BY status")}
        runs = {row[0]: row[1] for row in connection.execute(
            "SELECT status,COUNT(*) FROM runtime_runs GROUP BY status ORDER BY status")}
        active_runs = count("SELECT COUNT(*) FROM runtime_runs "
                            "WHERE status IN ('started','running','waiting_approval')")

        anomalies = {
            "conversation_notebook_missing": count("""SELECT COUNT(*) FROM conversations c
                LEFT JOIN notebooks n ON n.id=c.notebook_id
                WHERE c.notebook_id IS NOT NULL AND n.id IS NULL"""),
            "chunk_ancestry_inconsistent": count("""SELECT COUNT(*) FROM document_chunks dc
                LEFT JOIN canonical_documents cd ON cd.id=dc.document_id
                LEFT JOIN notebook_sources ns ON ns.id=dc.source_id
                WHERE cd.id IS NULL OR ns.id IS NULL OR dc.notebook_id!=cd.notebook_id
                OR dc.notebook_id!=ns.notebook_id OR cd.source_id!=ns.id"""),
            "citation_references_missing": count("""SELECT COUNT(*) FROM message_citations mc
                LEFT JOIN messages m ON m.id=mc.message_id
                LEFT JOIN notebooks n ON n.id=mc.notebook_id
                LEFT JOIN notebook_sources ns ON ns.id=mc.source_id
                LEFT JOIN canonical_documents cd ON cd.id=mc.document_id
                LEFT JOIN document_chunks dc ON dc.id=mc.chunk_id
                WHERE m.id IS NULL OR n.id IS NULL OR ns.id IS NULL OR cd.id IS NULL OR dc.id IS NULL"""),
            "citation_ancestry_inconsistent": count("""SELECT COUNT(*) FROM message_citations mc
                JOIN notebook_sources ns ON ns.id=mc.source_id
                JOIN canonical_documents cd ON cd.id=mc.document_id
                JOIN document_chunks dc ON dc.id=mc.chunk_id
                WHERE ns.notebook_id!=mc.notebook_id OR cd.notebook_id!=mc.notebook_id
                OR dc.notebook_id!=mc.notebook_id OR cd.source_id!=mc.source_id
                OR dc.source_id!=mc.source_id OR dc.document_id!=mc.document_id"""),
            "run_message_missing": count("""SELECT COUNT(*) FROM runtime_runs r
                LEFT JOIN messages m ON m.id=r.message_id WHERE m.id IS NULL"""),
            "interaction_lineage_mismatch": count("""SELECT COUNT(*) FROM runtime_interactions i
                JOIN runtime_runs r ON r.id=i.run_id WHERE i.conversation_id!=r.conversation_id"""),
            "approval_lineage_mismatch": count("""SELECT COUNT(*) FROM pending_approvals a
                JOIN runtime_runs r ON r.id=a.run_id
                LEFT JOIN messages m ON m.id=a.message_id
                WHERE a.conversation_id!=r.conversation_id OR m.id IS NULL
                OR m.conversation_id!=a.conversation_id"""),
            "legacy_trace_references_missing": count("""SELECT COUNT(*) FROM runtime_trace_events t
                LEFT JOIN conversations c ON c.id=t.conversation_id
                LEFT JOIN messages m ON m.id=t.message_id
                WHERE c.id IS NULL OR m.id IS NULL"""),
            "shadow_references_missing": count("""SELECT COUNT(*) FROM shadow_observations s
                LEFT JOIN conversations c ON c.id=s.conversation_id
                LEFT JOIN messages m ON m.id=s.message_id
                WHERE c.id IS NULL OR m.id IS NULL"""),
        }
        ambiguous_relationships = {
            "citations_have_unconstrained_resource_ids": resources["message_citations"],
            "legacy_trace_rows_have_no_foreign_keys": resources["runtime_trace_events"],
            "shadow_rows_have_no_foreign_keys": resources["shadow_observations"],
            "agent_profile_notebook_links": count("SELECT COUNT(*) FROM agent_profile_notebooks"),
            "conversation_notebook_links_have_no_foreign_key": count(
                "SELECT COUNT(*) FROM conversations WHERE notebook_id IS NOT NULL"),
        }
        operational_tables = sorted(table for table in tables
                                    if any(token in table.lower() for token in ("queue", "delegat", "retry")))
        active_approval_count = sum(approvals.get(status, 0) for status in ("pending", "approved", "executing"))
        pending_interaction_count = interactions.get("pending", 0)
        unowned_count = sum(item["unowned"] for item in ownership.values())
        anomaly_count = sum(anomalies.values())

        blockers = []
        if version != CURRENT_SCHEMA_VERSION:
            blockers.append("ownership_schema_missing")
        if admin_state != "bootstrap_completed" or not active_users:
            blockers.append("bootstrap_or_active_user_missing")
        if unowned_count:
            blockers.append("unowned_direct_resources_remain")
        if active_approval_count:
            blockers.append("approval_capabilities_pending_or_executing")
        if pending_interaction_count:
            blockers.append("interactions_pending")
        if active_runs:
            blockers.append("runs_active_or_waiting")
        if anomaly_count:
            blockers.append("resource_relationship_anomalies")
        if any(ambiguous_relationships.values()):
            blockers.append("unresolved_non_foreign_key_relationships")
        if not operational_tables:
            blockers.append("durable_queue_retry_delegation_state_not_represented")
        blockers.extend(("operator_consent_not_recorded", "backup_restore_not_verified",
                         "runtime_ownership_enforcement_not_implemented", "administrator_role_not_modelled"))

        return {
            "schema_version": version,
            "integrity": {"integrity_check": integrity, "foreign_key_violations": foreign_key_violations},
            "ledger": {"rows": count("SELECT COUNT(*) FROM schema_migrations"),
                       "contiguous": True,
                       "latest": count("SELECT MAX(version) FROM schema_migrations")},
            "administrative_state": {"state": admin_state, "active_users": active_users,
                                     "bootstrap_events": bootstrap_actors,
                                     "administrator_identity": "not distinguishable by a role column"},
            "resource_counts": resources,
            "resource_classification": {
                "direct_owner": ["conversations", "notebooks"],
                "inherited_owner": ["messages", "runtime_runs", "runtime_events", "runtime_interactions",
                                    "pending_approvals", "notebook_sources", "canonical_documents",
                                    "canonical_spans", "document_chunks", "message_citations"],
                "global_admin": list(_GLOBAL_TABLES),
                "ambiguous_or_external": ["shadow_observations", "runtime_trace_events",
                                           "attachments embedded in messages", "vector/Qdrant projection",
                                           "provider/tool/MCP effects already in flight"],
            },
            "ownership": ownership,
            "global_resource_counts": global_resources,
            "user_preference_counts": {
                "legacy_global": global_resources.get("user_preferences", 0),
                "per_user": count("SELECT COUNT(*) FROM auth_user_preferences")
                if "auth_user_preferences" in tables else 0,
            },
            "operations": {"approvals_by_status": approvals,
                           "interactions_by_status": interactions,
                           "runs_by_status": runs,
                           "active_runs": active_runs,
                           "durable_queue_retry_delegation_tables": operational_tables},
            "relationship_anomalies": anomalies,
            "ambiguous_relationship_counts": ambiguous_relationships,
            "observations": {
                "schema_manifest": "validated",
                "direct_owner_columns": "present" if version == CURRENT_SCHEMA_VERSION else "absent",
                "null_owner_semantics": "unassigned; never implies public or administrator access",
                "report_contents": "counts and states only; no row identifiers or payloads",
            },
            "blockers": blockers,
            "preparation_status": "BLOCKED",
            "activation_ready": False,
        }
    finally:
        connection.close()
