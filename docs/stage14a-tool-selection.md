# Stage 14A — deterministic tool selection

## Boundary and invariants

The existing path resolves registered tools through Agent intent, runtime availability/model capability, and `ExposurePolicy` into an immutable `EffectiveToolSet`. `select_tools` runs immediately after that resolution and before provider request construction. Its output is another `EffectiveToolSet`; it has no registry access and cannot add tools. Selection is relevance, never authorization. `ToolExecutor`, `PolicyEvaluator`, approval, and exact-call approval resume remain downstream and unchanged.

The selected names and metrics are stored in the existing per-run runtime snapshot. Approval continuation persists selected tool names and reconstructs precisely that set. ProgressGuard recovery turns reuse the same runtime's set; selection is not rerun. CHAT selection starts from CHAT's own authorized set and never reads Agent tool intent.

## Ranking

The selector is local, synchronous, and deterministic. It normalizes punctuation, tokenizes only the current user request plus at most four prior user/assistant messages (500 characters each), and scores authorized tools with these simple weights:

- exact normalized tool-name match: +100;
- each matched capability: +12;
- query-token overlap with tool name, description, or schema text: +2 per token, capped at +8;
- eligible Knowledge research sibling (`search`, `read`, browse/relationship/context family): +9;
- explicitly requested maintenance operation: +30.

Ties retain registry order. The capability vocabulary is bounded in `app/tool_selection.py`: knowledge, search, read, browse, relationships, maintenance, weather, web, datetime, visualization, user_input, infrastructure, diagnostics. MCP metadata is inferred locally and conservatively from stable tool ID/server identity, remote name, description, and schema; remote descriptions and definitions are not rewritten. Existing `ToolDefinition` fields already supply these inputs.

Knowledge intent includes a small research bundle, but never maintenance merely because search matched. Combined intents can select multiple families. `native.request_user_input` is not exposed on every turn; it is selected on explicit clarification/choice intent, retaining its normal runtime interaction behavior. Discovery questions expose the full authorized catalog so the model can accurately describe actual availability. Explicit naming only ranks tools present in the authorized set.

Greetings and other clear no-tool requests may select no tools. Unclassified/ambiguous research intent falls back to the full authorized set. Any unexpected selector exception is diagnosed safely and fails open to the full authorized set. Fallback cannot bypass authorization.

## Defaults and operation

- `NEXO_TOOL_SELECTOR=true` (enabled by default after deterministic acceptance).
- `NEXO_TOOL_SELECTOR_MAX_TOOLS=10` (bounded 1–100; discovery/fallback may exceed it to preserve recall).
- Context: four preceding user/assistant messages, 500 characters each.
- No cross-request cache, network calls, tokenizer, or persistent selector state.
- `max_tool_calls` remains an independent execution limit.

When disabled, the authorized set passes through unchanged. Agent Builder continues to store requested tool intent only; selection is turn-local.

## Diagnostics and benchmarks

Run snapshot/diagnostics include authorized and selected names/counts, serialized schema character counts, mode, capabilities, reason labels, fallback/discovery flags, and selector duration. Runtime trace emits bounded `tool_selection_started` and completed events with candidate/selected counts, duration, mode, and fallback. Schemas and arguments are never copied into diagnostics.

`tests/test_tool_selection.py` provides deterministic scenario coverage and computational scaling checks at 10, 50, 100, and 500 tools. Schema character count is a deterministic proxy, not a token estimate. Compare selector disabled/enabled from the same authorized set for catalog count, provider schema size, and required-tool recall. Live TTFT/quality claims require comparable live runs; schema reduction alone does not establish latency gains.

Local fixture evaluation (14 scenarios, 12-tool catalog): mean catalog reduction 68.5%, mean serialized-schema reduction 68.3%, zero fallback scenarios. A 30-sample local scaling run measured p95 selector times of 0.38, 1.71, 3.22, and 16.05 ms at 10, 50, 100, and 500 tools, respectively; these are machine-specific selector timings, not provider latency.

## Limitations

Classification is lexical and heuristic. Poorly described MCP tools may be over-selected through fallback or missed in ranked requests. Very large authorized catalogs remain fully exposed for discovery and conservative fallback. No learned routing, per-Agent policy, planning, or persistent affinity is introduced in this stage.
