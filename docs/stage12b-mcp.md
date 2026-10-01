# Stage 12B: MCP Foundation

NEXO connects to explicitly configured MCP servers over **Streamable HTTP** using the official Python MCP SDK. stdio, SSE-only transport, resources, prompts, OAuth, and subprocess execution are not supported. Endpoints must be HTTP(S), have no URL userinfo, query strings, or fragments, and be reachable from the NEXO container.

MCP server and discovered tool metadata live in `mcp_servers` and `mcp_tools`; server credentials live separately in `mcp_server_credentials` (schema migration 19). Discovery snapshots do not contain credentials. Tools default disabled and are exposed only when both the server and tool are enabled and the latest connection/discovery state is connected. IDs are deterministic: `mcp.<server-slug>.<remote-tool-name>`.

The `MCPManager` owns SDK initialization, discovery, calls, timeout enforcement, schema bounds, result normalization and safe failure responses. It adapts to the existing `ToolRegistry`; calls are executed by the existing AgentRuntime and ToolExecutor. Discovery is an explicit Settings action and does not restart NEXO. If discovery fails, the last tool metadata remains inspectable as stale but is no longer executable.

Settings supports no authentication or a static Bearer token. Bearer credentials live in the separate server-side `mcp_server_credentials` table in NEXO's SQLite data directory. They are never returned by server APIs or included in diagnostics/logs. The token input is write-only: blank keeps the stored token; switching auth to None removes it. Protect the data directory/backups as you do provider API keys. OAuth and external secret-store integrations are not supported.

Settings → Tools controls the maximum number of tool invocations per turn (1–50, default 10), independent of how many rounds or parallel calls the model uses. This is a safety ceiling shared by native, module, and MCP tools.

Text content is retained. JSON-serializable structured content is passed as `structured_data`; the runtime's existing tool-output character limit and MCP's 12,000-character limit bound model context and mark truncation. MCP results never automatically become artifacts; a model can call `native.render_artifact`. MCP HTML is treated as data, not executed/rendered HTML.

Diagnostics include server/tool IDs, duration, status/error category and truncation state only. They omit arguments, response bodies, authorization data and credentials. Settings exposes server CRUD, auth type, enable/disable, connect/refresh and per-tool enablement. There is no Lab inspector in this stage.
