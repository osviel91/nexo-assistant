# Shadow Evaluation

## Methodology

The fixture contains 25 deterministic Spanish prompts across explicit web, current information, timeless knowledge, coding, analysis, conversation, explicit tool, and ambiguous categories. Strong cases have curated labels; ambiguous cases are retained for calibration inspection and do not affect accuracy. The runner calls `DecisionRuntimeModule.shadow_decide`, which uses the same three production questions and one System One request.

Run with the production configuration:

```bash
PYTHONPATH=. .venv/bin/python scripts/evaluate_shadow.py
```

The runner writes `artifacts/shadow-evaluation.json`. It does not write conversations or include secrets.

## Results

The local run in this checkout completed the fixture with `decision_runtime_unavailable` for every case because no Arbiter URL/module configuration was present in the local shell. Therefore no accuracy, calibration, or meaningful latency result is claimed here. Run the command in the configured production-like environment to populate the machine-readable result.

| Dimension | Result |
| --- | --- |
| model | unavailable locally |
| cases | 25 |
| needs_web | NOT_READY pending configured run |
| needs_tools | NOT_READY pending configured run |
| task_type | NOT_READY pending configured run |
| boolean/noul mapping | verified by parser tests: `noul >= 0.5` maps to `true`, otherwise `false`; probabilities preserve `true` and `false` |
| latency | not measurable locally |

## Metrics

For strong labels, boolean scoring reports accuracy, false positives, false negatives, and confidence. Task scoring reports accuracy and an expected-to-observed confusion matrix. Confidence is split into correct and incorrect decisions, preserving the provider's original values. Latency reports min, p50, p95, and max for DecisionRuntime calls.

## Production state inspection

The production decision state is constructed in `DecisionRuntimeModule.shadow_decide` as:

```json
{"message": "<raw user text>", "available_tools": ["<registered tool names>"]}
```

The chat path passes `req.content` before attachment transformation, so attachments and assistant text do not replace the user's message. The same question builder is used by the evaluator. Tool names are sorted before the shadow request for deterministic input.

## Wording and taxonomy

The initial run must use the current production wording. Candidate wording changes are intentionally not applied. The taxonomy remains `conversation`, `knowledge`, `research`, `coding`, `analysis`, and `other`; systematic confusion should be recorded from the generated confusion matrix before any taxonomy change is considered.

## Decision gate

No dimension receives policy authority in Stage 4B. The local unconfigured run classifies all three dimensions as `NOT_READY`; a configured run may upgrade a dimension to `EXPERIMENTAL` or `CANDIDATE_FOR_POLICY` in this report only. This does not implement routing, web enablement, tool enablement, or model selection.

## Capability freshness

Configured providers are refreshed once at application startup through `GET /models`; the existing manual refresh remains available. Refreshes are not performed on chat requests. The last successful refresh is stored in `capability_refreshes` and returned as `capabilities_refreshed_at`. Failed startup refreshes retain the prior cache and emit a safe diagnostic.
