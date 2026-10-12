# NEXO Stage 15B.3B CP2-B.2b-2B.1: Historical Ownership Assignment Prototype

- Preflight: `HEAD=3a78b5334726dbf52c74970f68780d2e11c18e9a`, branch `main`, clean worktree; stash `b7d2662a55bb2519aadd4291a93400c689afb46c` preserved without applying.
- Mode: isolated Python prototype plus temporary SQLite fixtures. No production DB read, user creation, auth activation, runtime/API integration, or deployment.
- Production: **NO-GO**. Changes are local and uncommitted.

## Scope And Entities

`prototypes/historical_ownership_assignment.py` is not imported by `app.main`. It only assigns direct ownership in `conversations.owner_user_id` and `notebooks.owner_user_id`. Children retain their existing foreign-key/lineage relationships and inherit access conceptually; the prototype does not rewrite messages, runs, events, citations, notebook sources/documents/spans/chunks, traces, or shadow observations. Legacy `user_preferences`, `auth_user_preferences`, providers, profiles, tools, and all other global data are preserved untouched. The existing `conversation_tool_approvals` table has no runtime read/write references in inspected code and is preserved.

The source and backup paths must pass the existing `_temporary_database_path` policy, be regular non-symlink files outside configured `NEXO_DATA_DIR`, and carry `application_id=0x4E584F54`. The exact schema manifest, v31 ledger, `integrity_check`, and `foreign_key_check` are validated. Backup verification computes its SHA-256 and opens an isolated SQLite Backup-API restore, then checks the restored schema, data generation, integrity, and fixture marker. The backup must match the source generation before first assignment. The marker is a fixture guard only, not provenance/authentication: an operator can copy a DB and set its header.

## Identity, Consent, Inventory

The v31 model has no administrator-role field. The prototype requires exactly one active user, exactly one `bootstrap_completed` event naming that user, `auth_state='bootstrap_completed'`, and an explicit permission row for that target. This identifies the synthetic bootstrap actor; it does **not** prove a production admin role. The synthetic consent object must contain the exact fixture-only confirmation string, unique sorted conversation/notebook ID tuples, operation ID, target ID, timestamp, and a version-1 canonical JSON manifest whose supplied SHA-256 matches. This is test input, not a production consent mechanism or authentication proof.

The inventory hash is built from the verified backup's resource IDs/owner states, conversation-notebook links, operation-state vectors, lineage checks, and verified SQLite generation. The manifest hash binds operation, target and exact resource sets. Audit stores only these hashes/counts, safe backup filename ID/hash and SQLite data-generation digest; not row IDs, paths, contents, credentials, or secrets. Hashes do not establish authorization. The backup restore test is local synthetic evidence only.

## Preconditions And Invalidation Matrix

The function obtains the existing exclusive `DatabaseLease`, waits briefly for concurrent fixture operations, and opens `BEGIN IMMEDIATE`; it then re-inspects the locked source and compares its generation with the validated backup. After the write lock is held it rereads the full consented ID sets, owner values, statuses and relationship inventory, verifies exact ordered IDs/counts against the manifest, and performs owner/status conditional updates against those locked rows. A concurrent pre-lock change fails generation/manifest checks before writes. NEXO startup holds the corresponding shared lease for its process lifetime, so a cooperating local runtime prevents this offline lease. SQLite serializes any writer that reaches the DB. This coordination is not proof that an unrelated process/external executor obeys the lease.

| Observed stored state | Prototype treatment | Reason |
|---|---|---|
| Run `completed` or `failed` with terminal timestamp | Preserve | Terminal states emitted by the current runtime. Missing terminal timestamp is inconsistent and blocks. |
| Run `started`, `running`, `waiting_approval` | Block entire operation | Run may still have an in-memory stream/provider/tool effect. |
| Any other run state | Block | `runtime_runs.status` has no CHECK; unknown is ambiguous. |
| Approval `pending`/`approved` whose run is terminal | Conditionally set to existing allowed `invalidated` | Synthetic fixture has no executor, and the cooperating app process is excluded by lease. |
| Approval `executing` | Block | Effect may already be in flight; SQL cannot reverse or fence it. |
| Other documented terminal approval state | Preserve | No need to erase historical result. |
| Interaction `pending` with terminal run | Conditionally set to existing runtime state `denied`, with a negative response | Runtime waiter recognizes non-pending state and receives denial. |
| Interaction `approved`, `denied`, `expired` | Preserve | Already resolved/terminal. |
| Any unknown interaction/approval state, including on a retry | Block | Schemas do not constrain all interaction states; do not invent a transition. |
| Durable queue/retry/delegation table name | Block | State semantics are not available to this prototype. No such tables were found in v31. |
| In-memory tasks/provider/MCP/tool effects | Not inferable from SQLite; fixture has none | A runtime lease only coordinates NEXO processes that honor it; it is not distributed fencing. |

Critical lineage anomalies block: conversation→notebook missing link, notebook/document/source/chunk ancestry mismatch, citation references/ancestry mismatch, run/message/conversation mismatch, approval/interaction lineage mismatch, trace/shadow dangling references, and a global AgentProfile→private notebook link. Existing non-null conversation/notebook owners, unknown states, mismatched consent sets, multiple assignment operations, invalid identity/schema/backup/manifest or FK/integrity failures also block before writes. An ambiguous failure leaves source data unchanged.

The read-only CP2-B.2b-2A checker reports current structural blockers including no modeled administrator role, no durable queue/retry/delegation state, active/pending capability counts and unresolved non-FK relationships. It remains permanently `BLOCKED`; this prototype does not turn its report into operational readiness.

## Transaction And Idempotency

Within one transaction the prototype performs owner compare-and-set updates only for manifest IDs with `owner_user_id IS NULL`, checks row counts, invalidates safe terminal-run approvals, denies safe pending interactions, validates resulting owner sets/relationships/integrity, and inserts the v31 operation row through `record_historical_ownership_audit`. The v31 trigger automatically inserts the single `historical_ownership_assigned` event in that same transaction. The helper owns no commit/rollback. The prototype commits once; it reopens the DB and validates v31 manifest, ledger, FKs, integrity, operation/event association and terminal queue state before returning success.

An identical retry validates the immutable backup/manifest and exact committed audit payload, then verifies the assigned owner set and no remaining pending/executable capabilities; it returns the existing operation without writes. A changed payload/backup/target under the same ID is rejected. A different operation ID cannot claim the same history: existing operation rows or non-null ownership reject it. A second concurrent same-ID caller waits for the lease and returns the existing result; distinct IDs serialize and only the first can commit. The lease wait is bounded and fails closed if a runtime or writer keeps it.

## Recovery And Guarantees

Injected errors after ownership, after invalidation, after audit, before commit, during the second ownership write, and during audit insertion leave ownership, status changes, operation row and generated event rolled back. SIGKILL before commit reopens with original rows/ledger and no operation/event; rerun succeeds. SIGKILL after commit reopens with all terminal ownership/invalidations and one operation/event; rerun is read-only/idempotent. Both paths verify SQLite integrity, foreign keys and manifest v31. These establish SQLite transaction recovery on the tested local filesystem, not power-loss, media, distributed effects, or full-system backup recovery.

## Evidence And Limitations

Tests use only marked temporary synthetic files with synthetic identities, conversations, notebooks, one valid child lineage, runs, approvals, interactions and optional legacy preference. They cover valid assignment; path/schema/ledger/backup/manifest/identity/consent checks; absent, pre-existing and ambiguous ownership; lineage anomaly; pending, approved, executing, terminal, active and unknown states; preservation of completed/resolved state and preferences; SQL/fault-hook rollback; exact/different retry; same/different-ID concurrency; SIGKILL before/after commit; reopen and integrity checks. No test invokes provider/tool/MCP or accesses production data.

Evidence remains `prototype-verified` only. CP1's migration/auth gates are not promoted; there is no runtime authorization, ownership-aware query coverage, multi-user isolation, live API test or operational evidence. Most importantly, this fixture requires an already-`bootstrap_completed` synthetic target because v31 validation does not accept an identity in `schema_installed`/`bootstrap_pending`; it therefore tests the assignment sub-operation, not first-admin creation plus final bootstrap state in one real lifecycle. The schema has no admin role. We do not claim this prototype can safely quiesce external effect processors or prove real operator consent/backup custody.

## Integration Blockers

Before any later integration: decide and model admin role/identity without inferring from username/status; design an authenticated operator-consent binding to canonical inventory and backup/restore proof; coordinate process shutdown and every provider/tool/MCP/queue executor with real fencing; establish the pre-bootstrap transaction/state contract; resolve global AgentProfile→notebook sharing and legacy orphan/anomaly cases; test against the complete runtime repositories and every route before effects; define failure/recovery and external-effect reconciliation. The prototype is not a CLI for a deployed database and must not be pointed at production. CP2-B.2b-2B.2, CP3 and Stage 15C remain unstarted.

## Verification

Preflight HEAD: `3a78b5334726dbf52c74970f68780d2e11c18e9a`; branch `main`; stash `b7d2662a55bb2519aadd4291a93400c689afb46c` unchanged. Final test/gate counts and worktree state are recorded after executing this checkpoint's quality gates.

Code/schema inspection found the stored run states used by runtime (`started`, `running`, `waiting_approval`, `completed`, `failed`); only the last two are treated as terminal. `pending_approvals.status` has a CHECK allowing `pending`, `approved`, `rejected`, `executing`, `executed`, `failed`, `expired`, `invalidated`. `runtime_interactions.status` has no CHECK; runtime writes `pending`, `approved`, `denied`, and `expired`, so every other value blocks. No durable queue/retry/delegation table exists in v31; there are in-memory async streams/tasks. No live SQLite rows were inspected, so no production/preexisting counts or actual operation states are claimed.

## Final QA Recheck

The QA pass confirmed status transitions are existing runtime/schema values, active/unknown/executing states are checked before any DML, consented inventory and counts are re-read under the exclusive lease and `BEGIN IMMEDIATE`, and existing ownership is never overwritten. A retry after commit validates the stored payload and owner set without repeating assignment DML; changed inputs reject. Bootstrap actor/authority and external fencing limitations remain explicit; no claim was added that the lease covers non-cooperating executors.

Final verification: focused ownership-assignment + audit + preparation suites **28 passed** (5 existing FastAPI/Starlette deprecation warnings); `./scripts/nexo check`: **448 passed, 1 skipped**, exit 0 (same Python warnings and 3 Node `MODULE_TYPELESS_PACKAGE_JSON` warnings); `git diff --check` and whitespace for all new files are clean. No new schema migration was added. HEAD remains the expected preflight commit because delivery is uncommitted. Stash remains unchanged. The worktree contains only the assignment prototype, its tests, this report, and the CP1 evidence note.
