# Stage 12A: Native Tools and Artifacts

Native tools register through the same `ToolRegistry` and immutable effective
tool snapshot used by modules. Tool names are stable IDs; schemas, source,
module and capability metadata are available from the catalog. MCP is not
registered or integrated by this stage.

`native.get_current_datetime` uses Python `datetime` and `zoneinfo`. Omitted
timezone means UTC; invalid IANA names produce a normal tool error. `native.render_artifact`
validates version-1 declarative specs before returning them through `ToolResult`
artifacts. The agent emits each valid artifact during the existing SSE run and
the assistant message stores the JSON spec in SQLite. Branching copies it.

Supported specs are `table`, `bar`, `line`, `pie`, `scatter`, `metrics`, and
`html`. Native tables, metrics and SVG chart marks use NEXO semantic tokens;
there is no charting dependency or model-supplied JavaScript. Specs are versioned
and have generated stable IDs. Current bounds: five artifacts per turn, 500
table rows, 30 columns/metrics, 1,000 chart points, 50,000 HTML characters and
100,000 serialized characters per artifact. Tool output remains under the
existing runtime output limit.

HTML artifacts are untrusted. They are rendered only in an iframe with an empty
`sandbox` attribute and restrictive CSP (`default-src 'none'`, no forms, base
URL, scripts, or external resources); script elements are also removed before
storage and display. The iframe has no NEXO DOM, cookies, storage, auth state,
or navigation privileges. JavaScript is disabled.

Run metadata records tool/native-tool counts, artifact count and artifact types;
tool trace events retain durations and safe names. It never stores artifact
payloads in diagnostics. MCP, JavaScript artifacts, standalone dashboards,
sorting, charting libraries and general export remain deferred.
