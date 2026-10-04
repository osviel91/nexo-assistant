# Stage 14B — Agent convergence, turn boundaries, and structured results

## Scope and invariants

This stage changes how the existing Agent runtime interprets persisted turns, evaluates research progress, and presents large results to the model. It does not add planning, routing, tools, or execution budget. Tool authorization and selection remain upstream; `ProgressGuard` only stops repeated/low-progress execution. CHAT and AGENT remain separate, approval and interaction continuations retain their snapshots, and canonical tool results/artifacts remain unchanged.

## Current-turn boundary

The provider history query associates each message with its latest runtime-run status. A failed user request stays in conversation history, but is sent as a short system note labeled historical context rather than an active user command. A deterministic marker check keeps the latest failed request active when the new request clearly refers to it (for example, “continue”, “that search”, or “¿y para pasado mañana?”). Explicit topic changes and detected shifts between weather, date/time, web, visualization, and knowledge topics are considered when classifying follow-ups. The database transcript is never rewritten. The Agent also receives a brief instruction that the latest user message is the active task.

This is deliberately lexical: it does not infer arbitrary semantic references. Successful earlier turns remain available as context. The current turn and authorized tool snapshot continue to determine what can execute.

## ProgressGuard convergence

The existing guard retains its thresholds, one recovery, hard stop, bounded history, and configured `max_tool_calls`. It now records hashed search terms and hashed resource identifiers, and compares recent search-family calls for query overlap and weak evidence growth. Repeated truncation can contribute a bounded stagnation signal. Only hashes and aggregate counters enter persisted guard state; raw queries and result records are not added to traces.

Productive unique evidence still lowers stagnation; the regression workflow covers search, reading, and new evidence. Approval waiting does not count as execution. A rejection increments an approval-rejection counter but is not treated as failure/stagnation. `native.request_user_input` and artifact rendering are excluded from guard evidence.

When the configured call budget is reached, remaining proposed calls receive a paired “budget exhausted” tool result and the runtime requests one tool-less synthesis turn. No tool calls execute beyond the configured budget. The same tool-less synthesis path is used after the existing guard hard stop.

## Structured result presentation

`ToolResultPipeline` deep-copies a JSON-safe canonical result and creates a bounded presentation for model context. Generic compaction preserves useful schema and identity fields, limits nested collections and long strings, omits common binary/map/HTML payloads, ranks records by deterministic query-token overlap, and reports record/truncation metadata. A malformed or oversized result falls back to a bounded presentation. An optional `ToolDefinition.result_projector` can apply domain-specific presentation; AEMET uses the same bounded projector with requested query/date/location relevance, while MCP results use generic projection.

Artifacts are extracted before projection, and the original result remains canonical for runtime processing. The model receives only the projected representation; projection never changes tool authorization or execution.

## Observability and validation

Safe runtime events include `tool_result_projected`, `research_progress_evaluated`, and `forced_synthesis`, with aggregate sizes, record counts, evidence growth, low-progress, and truncation counters. Payloads, raw queries, and result content are excluded.

Deterministic tests cover failed-turn context and follow-ups, productive research and similar-search stagnation, approval rejection/resume state, budget-triggered synthesis, generic/MCP and AEMET projection, malformed results/projector failure, truncation bounds, and artifact preservation. Provider-specific behavior and live AEMET/MCP services require an environment with those services; fixture tests do not claim live validation.
