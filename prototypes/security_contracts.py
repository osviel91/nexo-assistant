"""Declarative CP1 contracts; this module does not protect the NEXO runtime."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Iterable


CATALOG_VERSION = 1


@dataclass(frozen=True)
class HttpContract:
    id: str
    method: str
    path: str
    registration: str
    policy: str
    permission: str | None = None
    ownership: bool = False
    csrf: bool = False
    requirements: tuple[str, ...] = ()
    module: str | None = None


# Permissions are explicit decisions, never inferred from HTTP method.
CATALOG: tuple[HttpContract, ...]
_items: list[HttpContract] = []


def _add(policy: str, permission: str | None, records: str, *, ownership: bool = False,
         csrf: bool = False, requirements: tuple[str, ...] = (), registration: str = "core",
         module: str | None = None) -> None:
    # Stable IDs are derived from catalog order; the tuple below is versioned and reviewed.
    for record in records.splitlines():
        method, path = record.split(" ", 1)
        _items.append(HttpContract(f"http.v{CATALOG_VERSION}.{len(_items) + 1:03}", method, path,
                                   registration, policy, permission, ownership, csrf,
                                   requirements, module))


for _method, _path in product(("GET", "HEAD"), ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")):
    _add("admin", "diagnostics:read", f"{_method} {_path}", registration="framework",
         requirements=("disable-in-production-allowed",))

_add("user", "chat:write", "POST /api/files", csrf=True, requirements=("authorize-before-body",), registration="module", module="attachments")
_add("admin", "diagnostics:run", "POST /api/decisions", csrf=True, requirements=("provider-egress-after-authorization",), registration="module", module="decision-runtime")
_add("admin", "diagnostics:run", "POST /api/settings/retrieval-benchmark", csrf=True, requirements=("notebook-before-job",))
_add("admin", "diagnostics:read", "GET /api/settings/retrieval-benchmark/{job_id}", ownership=True)
_add("admin", "config:read", "GET /api/settings/tools")
_add("admin", "config:write", "PUT /api/settings/tools", csrf=True)
_add("admin", "mcp:read", "GET /api/mcp/servers")
_add("admin", "mcp:write", "POST /api/mcp/servers", csrf=True)
_add("admin", "mcp:write", "PUT /api/mcp/servers/{server_id}", csrf=True)
_add("admin", "mcp:write", "PATCH /api/mcp/servers/{server_id}", csrf=True)
_add("admin", "mcp:connect", "POST /api/mcp/servers/{server_id}/connect", csrf=True, requirements=("provider-egress-after-authorization",))
_add("admin", "tools:write", "PATCH /api/mcp/servers/{server_id}/tools", csrf=True)
_add("admin", "tools:write", "PATCH /api/mcp/servers/{server_id}/tools/{tool_id:path}", csrf=True)
_add("admin", "mcp:write", "DELETE /api/mcp/servers/{server_id}", csrf=True)
_add("user", "agents:interact", "POST /api/interactions/{interaction_id}", ownership=True, csrf=True, requirements=("transactional-owner-cas", "authorize-before-effect"))
_add("user", "config:read", "GET /api/preferences")
_add("user", "config:write", "PATCH /api/preferences", csrf=True)
_add("public-liveness", None, "GET /api/health", requirements=("minimal-status-only",))
_add("admin", "config:read", "GET /api/settings/embeddings")
_add("admin", "config:write", "PUT /api/settings/embeddings", csrf=True)
_add("admin", "providers:test", "POST /api/settings/embeddings/test", csrf=True, requirements=("provider-egress-after-authorization",))
_add("admin", "providers:test", "POST /api/settings/embeddings/test-reranker", csrf=True, requirements=("provider-egress-after-authorization",))
_add("admin", "providers:write", "PATCH /api/providers/{pid}/models/{model_id:path}", csrf=True)
_add("admin", "config:read", "GET /api/modules")
_add("admin", "tools:read", "GET /api/tools")
_add("admin", "agents:read", "GET /api/agents")
_add("admin", "agents:write", "POST /api/agents", csrf=True, requirements=("validate-notebook-references",))
_add("admin", "agents:read", "GET /api/agents/{profile_id}")
_add("admin", "agents:write", "PATCH /api/agents/{profile_id}", csrf=True, requirements=("validate-notebook-references",))
_add("admin", "agents:write", "DELETE /api/agents/{profile_id}", csrf=True)
_add("admin", "rag:read", "GET /api/notebooks", ownership=True)
_add("admin", "rag:write", "POST /api/notebooks", ownership=True, csrf=True)
_add("admin", "rag:read", "GET /api/notebooks/{notebook_id}", ownership=True)
_add("admin", "rag:write", "PATCH /api/notebooks/{notebook_id}", ownership=True, csrf=True, requirements=("transactional-owner-predicate",))
_add("admin", "rag:write", "DELETE /api/notebooks/{notebook_id}", ownership=True, csrf=True, requirements=("transactional-owner-predicate", "external-effects-not-atomic"))
_add("admin", "rag:read", "GET /api/notebooks/{notebook_id}/sources", ownership=True)
_add("admin", "rag:read", "GET /api/notebooks/{notebook_id}/sources/{source_id}", ownership=True)
_add("admin", "rag:read_content", "GET /api/notebooks/{notebook_id}/sources/{source_id}/document", ownership=True)
_add("admin", "rag:read", "GET /api/notebooks/{notebook_id}/sources/{source_id}/index", ownership=True)
_add("admin", "rag:diagnostics", "GET /api/notebooks/{notebook_id}/sources/{source_id}/integrity", ownership=True)
_add("admin", "rag:write", "POST /api/notebooks/{notebook_id}/sources/{source_id}/ingest", ownership=True, csrf=True)
_add("admin", "rag:write", "POST /api/notebooks/{notebook_id}/sources/{source_id}/index", ownership=True, csrf=True, requirements=("authorize-before-provider-egress", "revalidate-before-persist"))
_add("admin", "rag:query", "POST /api/notebooks/{notebook_id}/retrieve", ownership=True, csrf=True, requirements=("authorize-before-provider-egress",))
_add("admin", "rag:diagnostics", "POST /api/notebooks/{notebook_id}/retrieve/diagnostics", ownership=True, csrf=True, requirements=("authorize-before-provider-egress",))
_add("admin", "rag:write", "POST /api/notebooks/{notebook_id}/sources", ownership=True, csrf=True, requirements=("authorize-before-body", "url-fetch-after-authorization"))
_add("admin", "rag:write", "DELETE /api/notebooks/{notebook_id}/sources/{source_id}", ownership=True, csrf=True, requirements=("transactional-owner-predicate", "external-effects-not-atomic"))
_add("admin", "diagnostics:read", "GET /api/lab/shadow", ownership=True)
_add("admin", "diagnostics:read", "GET /api/lab/diagnostics")
_add("admin", "diagnostics:read", "GET /api/lab/traces", ownership=True)
_add("admin", "diagnostics:read", "GET /api/lab/runs", ownership=True)
_add("admin", "diagnostics:read", "GET /api/lab/runs/{run_id}", ownership=True)
_add("admin", "diagnostics:read", "GET /api/lab/runs/{run_id}/events", ownership=True)
_add("admin", "diagnostics:read", "GET /api/lab/traces/{message_id}", ownership=True)
_add("admin", "providers:read", "GET /api/providers")
_add("admin", "providers:write", "POST /api/providers", csrf=True)
_add("admin", "providers:write", "PUT /api/providers/{pid}", csrf=True)
_add("admin", "providers:write", "DELETE /api/providers/{pid}", csrf=True)
_add("admin", "providers:test", "POST /api/providers/{pid}/refresh", csrf=True, requirements=("provider-egress-after-authorization",))
_add("admin", "providers:write", "DELETE /api/providers/{pid}/models/{model_id:path}", csrf=True)
_add("user", "chat:read", "GET /api/conversations", ownership=True)
_add("user", "chat:read", "GET /api/conversations/{cid}", ownership=True)
_add("user", "tools:approve", "POST /api/approvals/{approval_id}", ownership=True, csrf=True, requirements=("transactional-owner-cas", "authorize-before-tool", "persistence-before-effect"))
_add("user", "chat:delete", "DELETE /api/conversations/{cid}", ownership=True, csrf=True, requirements=("transactional-owner-predicate",))
_add("user", "chat:write", "PATCH /api/conversations/{cid}", ownership=True, csrf=True, requirements=("transactional-owner-predicate", "validate-references"))
_add("user", "chat:write", "POST /api/conversations/{cid}/branch/{message_id}", ownership=True, csrf=True, requirements=("transactional-owner-predicate", "rollback-descendants-on-failure"))
_add("user", "chat:write", "POST /api/chat", ownership=True, csrf=True, requirements=("authorize-before-domain-read-write", "authorize-before-task-hook", "authorize-before-sse-start", "reauthorize-before-each-effect"))
_add("public", None, "MOUNT /assets", registration="mount", requirements=("static-directory-only", "no-user-data-or-symlinks"))
_add("shell-ui-allowlist", None, "GET /{path:path}", requirements=("unknown-api-is-json-404", "no-public-wildcard"))

CATALOG = tuple(_items)


def validate_catalog(contracts: Iterable[HttpContract] = CATALOG) -> None:
    contracts = tuple(contracts)
    ids = [item.id for item in contracts]
    keys = [(item.method, item.path, item.registration, item.module) for item in contracts]
    if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
        raise ValueError("duplicate HTTP contract id or binding")
    for item in contracts:
        if item.policy in {"user", "admin"} and not item.permission:
            raise ValueError(f"permission required: {item.id}")
        if item.policy not in {"user", "admin"} and item.permission is not None:
            raise ValueError(f"unexpected permission: {item.id}")
        if item.csrf != (item.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
                         and item.registration != "framework" and item.policy not in {"public", "public-liveness"}):
            raise ValueError(f"CSRF contract mismatch: {item.id}")
        if item.registration == "module" and not item.module:
            raise ValueError(f"module name required: {item.id}")


def contracts_for_modules(modules: Iterable[str]) -> tuple[HttpContract, ...]:
    enabled = frozenset(modules)
    return tuple(item for item in CATALOG if item.module is None or item.module in enabled)


def coverage_errors(bindings: Iterable[tuple[str, str]], modules: Iterable[str]) -> tuple[str, ...]:
    """Compare method/path pairs; unknown and retired bindings are both drift."""
    expected = {(item.method, item.path) for item in contracts_for_modules(modules)}
    actual = set(bindings)
    errors = [f"missing {method} {path}" for method, path in sorted(expected - actual)]
    errors.extend(f"undeclared {method} {path}" for method, path in sorted(actual - expected))
    return tuple(errors)


EFFECT_COUNTERS = (
    "asgi_receive", "body_read", "multipart_parse", "fastapi_dependency", "domain_sqlite_open", "write",
    "run_create", "hook", "async_task", "tool", "mcp_invocation", "provider_egress", "stream_start",
)
OWNERSHIP_DOMAINS = (
    "conversations", "messages_and_branches", "notebooks", "sources_and_documents", "runs",
    "approvals", "interactions", "traces_and_diagnostics", "preferences", "agent_profile_notebook_refs",
)
OWNERSHIP_ACCEPTANCE = {
    domain: ("authorized-principal-owns-resource", "foreign-and-missing-indistinguishable",
             "owner-predicate-atomic-with-write")
    for domain in OWNERSHIP_DOMAINS
}
OWNERSHIP_ACCEPTANCE["approvals"] += ("one-shot-cas", "authorize-before-tool")
OWNERSHIP_ACCEPTANCE["interactions"] += ("one-shot-cas", "authorize-before-resume")
OWNERSHIP_ACCEPTANCE["agent_profile_notebook_refs"] += ("validate-reference-in-same-transaction",)
SSE_CHECKPOINTS = (
    "authorize_before_message_or_run_persist", "authorize_before_http_response_start",
    "reauthorize_before_each_protected_effect", "cooperative_revocation",
    "cancel_run_and_handle_disconnect", "scope_and_audience_limited_delegation",
    "conversation_ownership_during_execution", "storage_failure_stops_future_effects",
)
EVIDENCE_LEVELS = ("pending", "prototype-verified", "runtime-verified", "operational-verified")
INTEGRATION_REQUIREMENTS = (
    "catalog_and_guard_integrated", "domain_ownership_and_cas_integrated",
    "sse_and_effect_revalidation_integrated", "integration_acceptance_tests_pass",
)
RUNTIME_REQUIREMENTS = (
    "http_catalog_complete", "guard_connected", "admin_persistence_valid", "bootstrap_complete",
    "historical_ownership_resolved", "migrations_verified", "effective_policies", "sse_and_internal_ops",
    "unknown_routes_closed",
)
OPERATIONAL_REQUIREMENTS = (
    "https_and_topology_verified", "backup_restore_verified", "admin_recovery_available",
    "operational_approval",
)
MIGRATION_REQUIREMENTS = (
    "incomplete_migration_fails_closed", "ledger_commits_with_schema",
    "schema_validated_beyond_version", "unknown_future_version_rejected",
    "storage_failure_has_no_anonymous_fallback", "incompatible_restore_fails_closed",
    "bootstrap_requires_verified_schema_and_backup",
)


class SyntheticEffectHarness:
    """Counters and inert fake boundaries for isolated acceptance tests."""

    def __init__(self) -> None:
        self.counts = dict.fromkeys(EFFECT_COUNTERS, 0)

    def body_read(self) -> None:
        self.counts["body_read"] += 1

    def multipart_parse(self) -> None:
        self.counts["multipart_parse"] += 1

    def provider_call(self) -> None:
        self.counts["provider_egress"] += 1

    def tool_call(self) -> None:
        self.counts["tool"] += 1

    def mcp_call(self) -> None:
        self.counts["mcp_invocation"] += 1

    def assert_no_effects(self) -> None:
        if any(self.counts.values()):
            raise AssertionError(f"protected effects occurred: {self.counts}")


def integration_blockers(evidence: dict[str, str]) -> tuple[str, ...]:
    return tuple(name for name in INTEGRATION_REQUIREMENTS if evidence.get(name) != "runtime-verified")


def activation_blockers(evidence: dict[str, str]) -> tuple[str, ...]:
    required = INTEGRATION_REQUIREMENTS + RUNTIME_REQUIREMENTS + MIGRATION_REQUIREMENTS + OPERATIONAL_REQUIREMENTS
    return tuple(name for name in required
                 if evidence.get(name) != ("operational-verified" if name in OPERATIONAL_REQUIREMENTS
                                           else "runtime-verified"))
