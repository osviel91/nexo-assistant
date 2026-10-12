# Coding Agent Contract

1. Inspect the architecture and relevant tests before editing.
2. Use the canonical commands in `docs/development.md`; do not invent a new test command.
3. Run focused tests while implementing.
4. Run `./scripts/nexo check` before claiming verification.
5. Report **Implemented**, **Verified**, or **Production validated** precisely.
6. Never claim Production validated without deployment and exercise evidence.
7. Preserve the architecture invariants and backend/RAG contracts documented in `docs/development.md`.
8. Report intentionally skipped external checks, such as real Qdrant, provider, or deployment validation.

The supported environment is created with `./scripts/nexo bootstrap`. Do not
install ad hoc global packages to make a local check pass.

## Software Docs Governance

Use Software Docs Manager as the project's architectural change control, not as an after-the-fact notes archive.

1. Before an architecture-impacting increment, identify the correct workspace/project, inspect capabilities, list resources, and read relevant SHARED artifacts. Record the repository `HEAD` and worktree state; distinguish implemented behavior, prepared schema, prototypes/design, and deployment unknowns.
2. Reuse the single active owned MY WORK for the increment, or create one if none exists. Keep audit notes private. When changing a SHARED resource, create its private candidate at the same project-relative path; writes must name the MY WORK `contextId`. Direct reads/writes with a SHARED path may not resolve inside the private context; do not work around this by writing without `contextId`.
3. Follow the artifact intent rules: Markdown for rationale/status, Sequence for ordered execution, Event Flow only for evidence-backed asynchronous event causality, Conceptual for domain/system concepts, and Database for persistent structure. HTTP, SSE, synchronous calls, matching names, or service topology alone do not justify Event Flow, semantic identity, binding, or typed relationship.
4. Before authoring, read the relevant MCP DSL reference. Preserve Conceptual/Database stable IDs and exact entity anchors. Names are discovery hints, not evidence for bindings. Do not add inferred keys, cardinalities, ownership, message identity, causality, retry, or deployment facts.
5. Read the resource revision before replacement and use optimistic revision checks. Add concise descriptions/tags to new artifacts when useful. After authoring, validate the private context and render diagrams as appropriate. Treat validation errors as blockers; report warnings separately. Duplicate-title warnings can occur while a same-path candidate coexists with SHARED, but do not hide or mislabel them.
6. At a verifiable checkpoint, select only the intended architectural resource IDs for `submit_architectural_proposal`. Pass every intended retirement explicitly via `retireResourceIds`; omission is not retirement. Exclude audit notes and working checklists from the team-visible proposal unless explicitly requested. State the deterministic resource/dependency scope and any warnings.
7. Proposal-status surfaces can disagree. If the MCP proposal list conflicts with the UI or human-confirmed lifecycle, cross-check SHARED revisions and proposal-specific status/capabilities/preview where available; do not create a duplicate or withdraw another proposal based on an ambiguous listing alone. Never assume a generic capabilities response grants authority; the mutation endpoint rechecks authorization.
8. After submission, inspect and validate the immutable proposal and preview. Check `staleBase`, updates/creates/retires, and blockers. Do not approve, review, or promote unless a human explicitly authorizes that operation. A preview is not publication.
9. After human promotion, reread each affected artifact in SHARED, verify updates/additions/retirements and current revisions, then archive the associated MY WORK only with explicit human authorization. If the base advanced or any conflict appears, stop and reconcile against SHARED; never retry blindly.

Technical code verification and architectural consolidation are separate states. Report implementation, technical validation, proposal submission, and SHARED integration independently. Do not claim tests, deployment validation, or production state without evidence.
