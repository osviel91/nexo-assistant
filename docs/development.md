# Development Workflow

NEXO uses Python 3.12+, a pinned `requirements.txt`, static browser assets,
SQLite, and Docker Compose for the optional Qdrant integration. There was no
CI or canonical command before Stage 10F; local setup was an ad hoc venv plus
`pip install` and individual test commands.

## Canonical commands

Run these from the repository root:

```sh
./scripts/nexo bootstrap       # create/update .venv and install pinned dependencies
./scripts/nexo dev             # bootstrap and run Uvicorn on port 8787
./scripts/nexo test            # backend pytest suite plus all frontend .mjs tests
./scripts/nexo test-backend    # Python tests only
./scripts/nexo test-frontend   # Node syntax and frontend regressions only
./scripts/nexo check           # complete deterministic quality gate
./scripts/nexo smoke           # temporary SQLite + local vector store, GET /api/health
```

`check` is the command used to decide whether a change is verified. It runs:

1. `git diff --check` for whitespace errors.
2. `.venv/bin/python -m compileall -q app`.
3. `.venv/bin/python -m pytest -q tests --ignore='tests/*.mjs'`, including migrations, contracts, retrieval, reranking, grounding, agents, notebooks, golden scenarios, and the vector-store contract.
4. `node tests/*.mjs`, including JavaScript syntax and Stage 10D/10E product invariants.

Missing dependencies are errors. The scripts never silently fall back to the
system Python or skip a test family. `check` does not reject a dirty worktree;
it only checks diff hygiene.

## Configuration and services

`.env.example` describes safe local defaults. Deterministic development uses
`NEXO_VECTOR_STORE=local`, temporary SQLite databases in tests, fake embedding
providers, fake rerankers, and in-process vector indexes. It needs no provider
credentials, internet, Qdrant, or production database.

Docker Compose starts NEXO and Qdrant for external integration work. Qdrant is
derived and disposable; SQLite remains canonical. Use `docker compose up -d
--build` only for deployment/integration validation, not for the default
quality gate.

## Verification status

- **Implemented**: code exists, but the canonical quality gate has not completed.
- **Verified**: `./scripts/nexo check` completed successfully from the supported environment.
- **Production validated**: the deployed application was exercised and deployment evidence exists.

These statuses are not interchangeable. Never report Verified when bootstrap,
tests, or check failed. Never report Production validated from local tests.

## Test taxonomy

The existing test layout remains intentionally stable:

- Unit: focused module behavior such as grounding, query analysis, reranking, and policies.
- Integration: SQLite repositories, ingestion, retrieval, notebooks, agents, and runtime traces.
- Contract: migrations, provider serialization, module/kernel contracts, vector stores, and frontend structure.
- Golden: `tests/test_golden_scenarios.py`, plus Murphy retrieval/reranking scenarios in Stage 9 tests.
- Smoke/deployment: `./scripts/nexo smoke` and Docker/Portainer checks, explicitly external to the default gate.

## Architecture invariants

- Chat execution is distinct from Agent execution.
- `AgentProfile` owns Agent model and Soul authority.
- Notebook binding is explicit and persisted; inherited and explicit clear semantics remain distinct.
- SQLite is canonical knowledge and embedding state.
- Qdrant is a disposable, rebuildable dense index.
- Retrieval, relevance, and grounding are separate decisions.
- `KnowledgeOutcome` explicitly describes the generation knowledge state.
- Citations map to canonical source provenance.
- Query expansion can improve recall but cannot establish relevance on its own.
- The frontend follows the Stage 10E shell, token, primitive, theme, and responsive contracts.
