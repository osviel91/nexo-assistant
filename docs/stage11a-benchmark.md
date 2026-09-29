# Stage 11A Retrieval Benchmark

Retrieval telemetry is observational. It does not change ranking, candidate
limits, query analysis, embeddings, reranking, relevance, or grounding.

## Commands

```sh
./scripts/nexo benchmark retrieval
./scripts/nexo benchmark retrieval --repetitions 20 --output artifacts/benchmarks/retrieval.json
./scripts/nexo benchmark compare before.json after.json
./scripts/nexo benchmark retrieval --live --reranker-candidates 20,15,10,8,5
```

The deterministic benchmark uses controlled embedding and index doubles. Each
scenario records one cold execution, one warm-up execution, and repeated warm
executions. Results are written under `artifacts/benchmarks/`, which is
intended to remain untracked unless a baseline is deliberately selected.

Live provider/Qdrant measurements are not run by `check` and are not silently
substituted for deterministic measurements. `--live` explicitly uses the
configured embedding provider, Qdrant store, reranker, and the named notebook
(default `Sabiduría de Murphy`). It fails if those are unavailable. Candidate
limits are ephemeral runtime overrides and candidate 20 is included as the
control. Live runs record cold, warm-up, and repeated warm observations; runs
where the reranker did not apply completely are marked invalid and excluded
from latency summaries. Since a remote provider cannot be reset by this
process, the artifact records one cold observation only (the first candidate-20
direct-lexical run); it does not mislabel later calls as cold.

The repository does not contain persisted labels for real notebook chunk IDs.
For this fixed Murphy scenario set, the live harness builds a transparent
scenario-anchor gold set by scanning indexed chunk text (law/perversity/nature,
toast/butter, corollary, or their compound union); it records only IDs and
metrics, never text. A missing positive anchor is marked `gold_anchor_not_found`
and its scores remain null. Treat these lexical scenario labels as a bounded
gold set, not a general semantic relevance judgment. The benchmark does not
generate answers, so generated citation counts are not applicable.

## Artifact

The JSON contains `benchmark_version`, `timestamp`, `git_commit`, `mode`,
`environment`, `configuration`, and `scenarios`. Each scenario contains cold,
warm-up, and warm rows, latency statistics (`count`, `min`, `median`, `p50`,
`p95`, `max`, `mean`), candidate counts, timing fields, quality metrics, and
expected/actual knowledge outcome and citation provenance.

Telemetry is limited to timings, implementation identity, counts, and safe
identifiers. It never includes prompts, document content, embeddings,
credentials, or provider URLs.
