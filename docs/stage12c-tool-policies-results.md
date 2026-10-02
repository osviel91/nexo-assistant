# Stage 12C: Tool Policies and Result Pipeline

Tools declare an `action` on `ToolDefinition`: `read_only`, `mutating`,
`destructive`, or `None` for unclassified. AgentRuntime's `PolicyEvaluator`
allows only `read_only`; mutations and unknown actions return a structured
`approval_required` result before invoking the handler. Approval pauses are
not resumable in the current SSE protocol, so this stage safely blocks rather
than asking for approval after execution. The contract contains tool ID/name,
classification, reason and run ID, never arguments. A repeated identical
blocked call terminates the run. Every requested invocation, including blocked
calls, consumes the existing per-run call budget.

The current native datetime, local artifact renderer, web search and AEMET
read tools are explicitly `read_only`. MCP actions are operator-controlled and
persist in migration 21; existing rows default to `unknown`. The Settings MCP
tool list exposes that classification alongside enabled state. Unknown,
mutating and destructive tools require the same approval boundary.

Every handler result is normalized and passed through `ToolResultPipeline`
before model context. The canonical JSON-safe result is retained for runtime
handling, while the model receives a deterministic character-bounded
projection (default 12,000 characters, configurable with
`NEXO_MAX_TOOL_OUTPUT_CHARS`). Large arrays are shortened while preserving
object fields and representative records; `result_metadata` reports omission,
original size and projected size. Plain text is truncated only with explicit
truncation metadata. Artifacts are emitted/stored before projection, and their
payloads are excluded from the model result.

MCP retains protocol parsing, timeout and safe error handling but no longer
truncates generic result content. AEMET retains API response byte limits,
map/base64 filtering and optional domain filtering; common runtime bounds apply
after its adapter returns. MCP/adapter diagnostics continue to omit arguments,
credentials and response content. Human approval/resume, per-agent policies,
and broader context budgeting remain deferred.
