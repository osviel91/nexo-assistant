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
