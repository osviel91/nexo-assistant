# Stage 12B: MCP Foundation

NEXO connects to explicitly configured MCP servers over **Streamable HTTP** using the official Python MCP SDK. stdio, SSE-only transport, resources, prompts, OAuth, and subprocess execution are not supported. Endpoints must be HTTP(S), have no URL userinfo, and be reachable from the NEXO container.

MCP server and discovered tool metadata live in `mcp_servers` and `mcp_tools` (schema migration 18). Discovery snapshots do not contain credentials. Tools default disabled and are exposed only when both the server and tool are enabled and the latest connection/discovery state is connected. IDs are deterministic: `mcp.<server-slug>.<remote-tool-name>`.

The `MCPManager` owns SDK initialization, discovery, calls, timeout enforcement, schema bounds, result normalization and safe failure responses. It adapts to the existing `ToolRegistry`; calls are executed by the existing AgentRuntime and ToolExecutor. Discovery is an explicit Settings action and does not restart NEXO. If discovery fails, the last tool metadata remains inspectable as stale but is no longer executable.

**Credential limitation:** NEXO has no safe general-purpose credential-reference store for arbitrary MCP servers. Stage 12B therefore supports unauthenticated endpoints only and rejects URL userinfo, query strings and fragments. Add a secret-store integration before supporting authenticated MCP servers.

Text content is retained. JSON-serializable structured content is passed as `structured_data`; the runtime's existing tool-output character limit and MCP's 12,000-character limit bound model context and mark truncation. MCP results never automatically become artifacts; a model can call `native.render_artifact`. MCP HTML is treated as data, not executed/rendered HTML.

Diagnostics include server/tool IDs, duration, status/error category and truncation state only. They omit arguments, response bodies, authorization data and credentials. Settings exposes server CRUD, enable/disable, connect/refresh and per-tool enablement. There is no Lab inspector in this stage.
