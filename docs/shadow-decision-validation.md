# Shadow Decision Validation

## Configuration

Shadow mode is disabled by default. Enable both capabilities explicitly:

```text
NEXO_MODULES=attachments,decision-runtime
NEXO_DECISION_SHADOW=true
NEXO_ARBITER_URL=http://arbiter:8000
NEXO_DECISION_MODEL=jev-latest
NEXO_DECISION_TIMEOUT=10
```

Enabling `decision-runtime` alone does not enable shadow mode. If Arbiter is
unavailable, Nexo keeps serving normal chat and records a safe failure
observation when shadow is configured.

## Architecture

The current user message starts two independent branches. `AgentRuntime` keeps
its existing request payload and execution policy. The shadow branch calls the
existing `DecisionRuntime` once with the current message and a normalized list
of available tool names. Its result is never used to select a model, provider,
prompt, tools, parameters, web search, or tool rounds.

The normal stream remains the only source of the user-visible response. The
shadow task waits for the normal loop's execution facts before writing its
observation, without adding a required SSE event.

## Decision Schema

One request contains:

- `needs_web`: boolean
- `needs_tools`: boolean
- `task_type`: one of `conversation`, `knowledge`, `research`, `coding`, `analysis`, `other`

`complexity` and operational thresholds are intentionally absent.

## Execution Observation

The agent loop records facts, not intent:

```json
{"tools_used": ["web_search"], "tool_rounds": 1, "web_search_used": true}
```

Predicted answers retain their value, confidence, and probabilities. No
automatic accuracy or correctness score is calculated.

## Persistence and API

Observations are stored in the existing SQLite database in
`shadow_observations`, associated with the conversation and user message IDs.
The record contains prediction, model, metadata, latency, execution facts,
status/error, and timestamp. It does not duplicate the message text or store
headers, authorization, API keys, provider secrets, prompts, or raw requests.

`GET /api/lab/shadow?conversation_id=...&limit=20` returns recent normalized
records. The Lab's `Shadow` tab renders the same contract in standard and
developer themes.

## Validation

Automated coverage includes:

- existing decision runtime and agent tests;
- one multi-question shadow call;
- preservation of confidence/probabilities;
- recorded message association and execution facts;
- absence of API keys and authorization data from observations;
- agent tool-loop facts (`tools_used`, `tool_rounds`).

The repository test suite passed with `.venv/bin/python -m pytest -q` after the
implementation: 41 tests passed. The system Python baseline could not load the
project dependencies.

Representative manual cases for a real Arbiter run:

1. `Explícame qué diferencia hay entre TCP y UDP.`
2. `Busca qué novedades importantes ha habido esta semana en Qwen.`
3. A request that requires one of the configured tools.

No real Arbiter E2E was run in this environment because no reachable Arbiter
endpoint and key were configured. Consequently, no latency or model result is
claimed here. The stored `latency_ms` field records actual observations when
the integration is run.

## Remaining Risks

- Shadow observations are local application data and have no retention policy yet.
- Background persistence remains best-effort if the process exits during a response; normal stream completion and cancellation now release execution facts and persist the observation.
- Three examples are not enough to establish decision quality; analysis belongs
  to a later stage.
