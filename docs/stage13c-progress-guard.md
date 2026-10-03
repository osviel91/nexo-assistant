# Stage 13C: Runtime Progress Guard

The shared `AgentRuntime` tracks bounded, per-run tool-call and projected-result fingerprints. Calls are canonicalized by sorted JSON object keys and whitespace-normalized strings; unknown tool argument semantics are not guessed. Results are fingerprinted only after `ToolResultPipeline` projection. The 64-entry history stores hashes, safe failure codes, and durations, never raw arguments or results.

Repeated calls, repeated projected results, repeated failures, and two-/three-step cycles contribute to a decaying score. The first threshold crossing adds one short recovery instruction; a continued exact retry is blocked and the model receives one tool-less turn to answer from gathered evidence. The configured tool-call budget remains a hard ceiling. The pre-existing artifact-render retry limit retains precedence.

`NEXO_PROGRESS_GUARD` controls the feature (`true` by default; set `false`, `0`, `off`, or `no` to disable). The guard is runtime-local; approval continuations serialize its bounded state, and branch runs start new runtime state. Waiting for approval or user input does not add a failed result or stagnation score. Existing action policy and frozen approval arguments remain authoritative.

Runtime events contain only aggregate counters and timing. No tool arguments, result contents, or fingerprints are emitted. `guard_overhead_ms` is tracked separately from tool duration. `tests/test_stage13c_progress_guard.py` provides deterministic productive research, pagination, repeated call/result/failure, cycles, approval resume, and state-bound checks. Its local timing assertion is a regression bound, not a causal latency benchmark.

Current heuristic ceiling: result identity is based on the projected result and has no tool-specific semantic equivalence. Similar-but-not-identical results may be missed by design. Tune thresholds only against representative Vault and adapter traces; never relax policy or the configured max-call budget to improve completion rates.
