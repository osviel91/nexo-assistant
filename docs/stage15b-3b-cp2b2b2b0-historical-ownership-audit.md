# NEXO Stage 15B.3B CP2-B.2b-2B.0: Historical Ownership Audit Contract

- Preflight: `HEAD=9eeb1bceeeb2790e4b5e0b5927122773d2f553c9`, branch `main`, clean worktree; stash `b7d2662a55bb2519aadd4291a93400c689afb46c` preserved.
- Result: schema v31 and a transaction-scoped audit writer demonstrated on synthetic SQLite only. No ownership assignment, pending-operation invalidation, bootstrap, auth activation, endpoint, or production DB operation.
- Production: **NO-GO**. Changes remain uncommitted; no deploy/push/merge.

## Prior Audit State

The approved 2A blueprint requires ownership, pending-operation invalidation, audit, and final bootstrap state in one SQLite transaction. CP2-B.2b-2A found that v30's `auth_security_events` allowlist did not include this event and only had actor/time/correlation fields. The v30 table is a strict CHECK constraint protected by append-only UPDATE/DELETE triggers. v30 was the last schema version at preflight. The existing `admin_persistence.py` prototype uses a separate temporary-only store, has a fixed event allowlist, and does not establish a production-compatible audit contract.

Repository compatibility check: HEAD/rama/worktree matched the requested preflight; the historical stash hash was present at `stash@{0}`. The reviewed artifacts were the 2A blueprint, CP2-B.2a administrative schema, CP2-B.2b-1 ownership schema, CP2-B.2b-2A preparation report, `app/safe_migrations.py`, prototype persistence, and CP1 contracts. No repository evidence contradicted the approved blueprint. No Software Docs Shared artifacts were modified.

## Event Contract

The allowlisted event type is `historical_ownership_assigned`. Existing `auth_security_events` remains the event stream and preserves its original columns, index, and append-only triggers. Its `correlation_id` is the unique operation ID; actor is the exact target user and timestamp is shared with the operation record.

`auth_historical_ownership_operations` contains one committed operation per `operation_id` (PRIMARY KEY): `target_user_id` (FK `auth_users` RESTRICT), `created_at`, assigned conversation/notebook counts, SHA-256 references for pre-assignment inventory and assignment manifest, backup identifier and SHA-256, SQLite generation identifier, audit contract version (1), and terminal status (`committed`). Triggers reject update/delete, automatically create the event after a complete operation row is inserted, and reject direct insertion of an event unless the matching operation exists. A partial UNIQUE index on non-NULL historical-event `correlation_id` prevents two such events for one operation. Thus ordinary SQL cannot commit either a completed operation without its event or duplicate assignment events. The operation table is warranted because the current event row cannot carry counts, manifest/backup references, schema contract version, or final operation state. It stores no per-resource identifiers.

Required values are all fields in the operation record. Counts are nonnegative; hashes are exactly 64 lowercase hexadecimal characters; IDs and generation/backup references are nonempty. There are no optional payload fields. A hash is an integrity comparison value, not proof that a manifest was authorized or that a backup came from the claimed source. Backup identity, integrity hash, restore test, and storage path are distinct facts: only ID/hash are recorded here; restore evidence must be verified before entering the assignment transaction; sensitive paths are excluded.

The event/operation stores no conversation/document/message content, passwords, provider credentials, tokens, secrets, or historical resource IDs. SQLite trigger protection is ordinary DML protection only: a schema/file owner can remove triggers or edit the DB. This is not cryptographic immutability.

## Migration

v31 is registered in `TRANSACTIONAL_MIGRATIONS`; versions 1–30 are untouched. It rebuilds only `auth_security_events`: drop its index, rename the old table, create the widened CHECK, copy every row with original IDs/actor/timestamp/correlation, drop the old table, recreate the index and append-only triggers, and create the operation table plus its consistency trigger. DDL and v31 ledger insertion execute in the existing `BEGIN IMMEDIATE` unit. The exact v31 manifest captures the rebuilt table, constraints, FK, table, index, and triggers. v28, v29, and v30 remain recognized runtime schemas; v31 is the current target. No ownership data is updated by this migration.

## Atomicity, Idempotency, Concurrency

`record_historical_ownership_audit(connection, audit)` requires an already-open caller transaction. It inserts only the terminal operation row; a schema trigger creates exactly one matching event as part of that statement/transaction. The future caller must use one `BEGIN IMMEDIATE` transaction for precondition revalidation, domain ownership writes, pending-operation invalidation, this audit writer, and final operation state; it must commit once. Any SQL/audit/validation error, failed commit, or process death before commit rolls back all writes. The helper itself does not begin/commit, modify ownership, invalidate anything, or validate production preconditions. Its synthetic test uses a harmless title update as the stand-in domain write.

`operation_id` is the retry key and primary key. An identical confirmed retry returns `False` after verifying its event exists and makes no duplicate event; parameter mismatch is rejected. A partial preparation or precommit failure leaves no committed row and can be retried after revalidation. A committed mismatch is a conflict and must not be silently repeated. SQLite's single-writer serialization plus `BEGIN IMMEDIATE` and the unique key provide the concurrency boundary; future code must acquire the write transaction before checking for an existing operation. A failed operation is not durably recorded as a failed assignment: doing so separately would break the one-transaction contract; diagnostic logs must not be represented as authoritative status.

## Manifest, Backup, Recovery

The inventory hash identifies the canonical pre-assignment inventory manifest; the assignment-manifest hash identifies the approved counts/scope and target. The target user FK records the destination identity. `sqlite_generation` identifies the canonical DB generation used for the operation. The external backup is independently checked for integrity and restore before the transaction; its ID and hash link the event to that backup without storing a path. Neither hash establishes authorization, provenance, or successful restore by itself.

On precommit failure/SIGKILL, SQLite recovery must show none of the transaction's synthetic write, operation row, or event. After commit/SIGKILL, all audit rows and synthetic write are present exactly once. A retry checks exact values; it does not rerun writes. If post-commit inspection finds mismatch, stop and preserve the DB/backup for recovery rather than retrying blindly. This does not promise recovery from power loss or filesystem/controller faults.

## Tests And CP1 Evidence

`tests/test_cp2b2b2b0_historical_ownership_audit.py` uses disposable SQLite only. It covers v31 manifest drift for CHECK/PK-UNIQUE/FK/index/trigger objects, v30→v31 promotion interrupted by SIGKILL after replace and recovery via `SafeMigrationEngine`, preservation of historical events, rejection of unknown/incomplete events, required fields/hash format, admin FK, audit statement/commit failure rollback, exact and mismatched retry, duplicate operation ID, two concurrent identical attempts, and SIGKILL before/after a synthetic transaction commit. Synthetic conversations/notebooks remain `owner_user_id IS NULL`; no pending approvals/interactions are changed. Existing tests confirm v28/v29/v30 runtime schema recognition and auth state remains `schema_installed` unless synthetic setup changes it. The focused combined CP1/CP2 suites passed **59 tests** before this QA update; see the QA addendum for final counts.

The evidence is `prototype-verified` only: isolated persistence/migration tests establish the contract shape, not an integrated assignment runtime. CP1 `bootstrap_requires_verified_schema_and_backup` remains `prototype-verified`; storage-error/no-anonymous-fallback remains `prototype-verified`; no evidence is promoted to `operational-verified`. No auth, endpoints, or middleware were changed. A synthetic backup/restore specifically for this event contract is not claimed by this checkpoint; existing backup/restore tests exercise the migration framework separately.

## Limits And CP2-B.2b-2B.1 Gates

These database invariants assume schema/triggers/indexes have not been tampered with. SQLite does not defer arbitrary cross-table checks to commit; here the one-way relation is guaranteed by operation-insert trigger plus uniqueness, not by a declarative cross-table FK. An actor able to alter schema, drop triggers/indexes, or edit the file can bypass the guarantees. The helper also assumes the caller rolls back after an unsuccessful `commit()`; it deliberately never commits or rolls back on the caller's behalf.

This does not provide authorization, administrator-role proof, operator consent, quiescence/fencing, manifest generation/signing, backup custody or restore proof, live-resource precondition checks, ownership writes, invalidation of pending approvals/interactions/runs, external-effect reconciliation, bootstrap completion, runtime ownership filters, or exposure readiness. A stored event does not authorize historical assignment or enable authentication.

Before 2B.1 assignment implementation: define the operation-manifest canonicalization and approval binding; establish exact administrator identity/role and operator consent; verify backup restore and SQLite generation; stop/fence writers and external effects; resolve active/ambiguous operations and orphaned relationships; implement all writes/status/event in one transaction; test concurrent identical/different retries against that writer; test migration/operation SIGKILL, restore, and error injection; and independently review recovery and CP1 evidence. Production remains NO-GO.

## QA Final CP2-B.2b-2B.0

The final QA review strengthened the schema so inserting a committed operation automatically creates its event; a partial UNIQUE index rejects another assignment event with the same operation ID. The matching-event trigger, operation/event append-only triggers, operation status CHECK, and the writer's retry verification jointly cover both association directions under ordinary SQL. The helper still leaves commit/rollback to its caller.

Added focal tests for v31 manifest drift (CHECK/PK-UNIQUE/FK/index/trigger), commit failure rollback, direct database event generation/duplicate rejection, and the full v30→v31 promotion interrupted by SIGKILL after replace then recovered by `SafeMigrationEngine`. That recovery test verifies exact prior event preservation and contiguous v31 ledger. The test exposed and fixed the safe engine's inability to continue from an already-validated v30 source: transactional migrations at or below the source version are now skipped in the staging loop.

QA results: `tests/test_cp2b2b2b0_historical_ownership_audit.py` **8 passed**; `./scripts/nexo check` **433 passed, 1 skipped**, 5 known FastAPI/Starlette deprecation warnings and 3 Node module-type warnings; `git diff --check` is required clean at handoff. Historical v28/v29/v30 validation remains available and auth state remains unchanged by migration. Stash preserved; no production SQLite, commit, push, merge, deployment, ownership assignment, invalidation, bootstrap, endpoint, or auth activation was performed.
