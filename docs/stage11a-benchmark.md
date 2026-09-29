# Stage 11A Retrieval Benchmark

Retrieval telemetry is observational. It does not change ranking, candidate
limits, query analysis, embeddings, reranking, relevance, or grounding.

## Commands

```sh
./scripts/nexo benchmark retrieval
./scripts/nexo benchmark retrieval --repetitions 20 --output artifacts/benchmarks/retrieval.json
./scripts/nexo benchmark compare before.json after.json
```

The deterministic benchmark uses controlled embedding and index doubles. Each
scenario records one cold execution, one warm-up execution, and repeated warm
executions. Results are written under `artifacts/benchmarks/`, which is
intended to remain untracked unless a baseline is deliberately selected.

Live provider/Qdrant measurements are not run by `check` and are not silently
substituted for deterministic measurements. A live run requires a future
explicit adapter for the configured environment.

## Artifact

The JSON contains `benchmark_version`, `timestamp`, `git_commit`, `mode`,
`environment`, `configuration`, and `scenarios`. Each scenario contains cold,
warm-up, and warm rows, latency statistics (`count`, `min`, `median`, `p50`,
`p95`, `max`, `mean`), candidate counts, timing fields, quality metrics, and
expected/actual knowledge outcome and citation provenance.

Telemetry is limited to timings, implementation identity, counts, and safe
identifiers. It never includes prompts, document content, embeddings,
credentials, or provider URLs.
