# Stage 13A — Agent runtime binding

`AgentProfile` remains the canonical persisted Agent. Migration 22 adds an
Agent-owned `max_tool_calls` (default 10, bounded 1–50) and the
`agent_profile_notebooks` relation; existing profile IDs, fields, and
`agent_profile_tools` selections are preserved. Notebook deletion cascades its
Agent binding.

At run time, an explicit request Notebook wins, followed by an existing
conversation Notebook, then Agent bindings. Zero Agent bindings means no
Agent-derived Notebook; one is effective; multiple without an explicit or
conversation choice return `409 agent_multiple_notebooks_unsupported`.
Retrieval remains single-Notebook. CHAT continues to use its request and
conversation binding only.

Agent-selected module/MCP tools remain persisted in `agent_profile_tools`.
Registered native tools are intrinsic to eligible Agent runs (tool toggle on
and model supports tool calling); they are not persisted as Agent selections.
Exposure still applies runtime toggles, availability, and model capability
before constructing provider `tools[]`. Execution policy remains a separate
PolicyEvaluator decision.

An Agent run snapshots resolved identity, inference, Notebook, tool sets, and
runtime budget in safe runtime metadata. Secrets, schemas, arguments, results,
and document contents are excluded. Existing global `max_tool_calls` remains
the CHAT budget; Agent runs use their persisted per-Agent value.
