# Nexo Chat

Self-hosted AI chat for OpenAI-compatible providers, with provider-level model discovery and per-message model selection. Initial target: Portainer on ZimaOS, with inference remaining on the oMLX node.

## Run locally

```sh
docker compose up -d --build
```

Open `http://<zimaos-ip>:8787`. Add a provider under **Providers** using its OpenAI-compatible base URL (for oMLX, the URL reachable from the ZimaOS host/container, ending in `/v1`). API keys are stored server-side in the persistent SQLite database. Model discovery uses `GET /models`; models can also be entered manually.

## Current scope

- Multiple providers and models per provider; refresh model list or add model IDs manually.
- Streamed chat, persisted conversations, and model/provider choice per assistant response.
- Image attachments are sent as data URLs. The selected model must support vision.
- PDF, DOCX, and text files are extracted and included as conversation context; images are sent to vision-capable models. This is a basic attachment flow, not a persistent NotebookLM-style knowledge base.
- Responsive web interface; single container; persistent `/data` volume.

## Modular foundation

The backend kernel owns startup, SQLite, the base chat/streaming flow, and the
small versioned module contract in `app/kernel.py`. Built-in modules are
registered explicitly in `app/main.py`; remote or hot-loaded plugins are not
supported.

The first module is `attachments`, which owns `/api/files` and the composer
attachment control. It is enabled by default to preserve existing behavior.
Set `NEXO_MODULES` to a comma-separated list to activate modules, or leave it
empty to run without optional modules:

```sh
NEXO_MODULES= uvicorn app.main:app --host 0.0.0.0 --port 8787
NEXO_MODULES=attachments docker compose up -d --build
```

### SearXNG web search (Etapa 2)

`web-search-searxng` is optional and disabled by default. It registers the
`web_search` tool only when all of these conditions hold: the module is listed
in `NEXO_MODULES`, `NEXO_SEARXNG_URL` is a reachable absolute HTTP(S) URL, and
the selected model explicitly advertises `tool-calling` in its `/models`
response (`capabilities`, `supports_tools`, or `tool_calling`). Unknown model
capabilities do not enable the tool.

Portainer environment example:

```text
NEXO_MODULES=attachments,web-search-searxng
NEXO_SEARXNG_URL=http://searxng:8080
NEXO_SEARXNG_LANGUAGE=all
NEXO_SEARXNG_SAFESEARCH=1
NEXO_SEARXNG_MAX_RESULTS=5
NEXO_SEARXNG_TIMEOUT=10
```

Use the Docker-network hostname or address reachable by the Nexo container;
`localhost` points to Nexo itself. The module sends only `q`, `format=json`,
`language`, and `safesearch`, returns at most 10 validated results, does not
download result pages, and stops tool execution after three rounds. Missing
URL, timeout, HTTP errors, invalid JSON, and malformed responses are reported
as search errors rather than empty results. No SearXNG instance or credential
is deployed by this stack.

Manual smoke test after deployment: set the variables above, rebuild, confirm
`web-search-searxng` appears in `GET /api/modules`, refresh providers, and
select a model whose `/models` entry advertises tool calling. Ask for a current
fact and verify that the streamed answer shows only the sources it cites. Use
the Docker-network address from Nexo; `localhost` works only when SearXNG is in
the same network namespace.

`GET /api/modules` exposes only active modules and their interface extensions.
Modules declare an id, version, kernel API version, capabilities and optional
dependencies. Lifecycle and chat hooks are finite and failures are isolated
and logged without exposing secrets or blocking the core.

### MCP tools (Etapa 3A)

MCP is optional and disabled unless `mcp` is included in `NEXO_MODULES`. Nexo
uses the official Python SDK with Streamable HTTP; it does not launch MCP
processes or deploy servers. Configure servers as a JSON array in
`NEXO_MCP_SERVERS`:

```text
NEXO_MODULES=attachments,web-search-searxng,mcp
NEXO_MCP_SERVERS=[{"id":"filesystem","url":"http://mcp-filesystem:8000/mcp","enabled":true,"allowed_tools":["read_file","list_directory"],"timeout":15}]
```

`allowed_tools` is mandatory in practice: an omitted or empty list exposes no
tools. Exposed names are deterministic and namespaced as
`mcp__<server_id>__<tool_name>`, so servers cannot overwrite native tools or
each other. Each `tools/call` has the configured timeout, and connection,
protocol, permission, execution and timeout failures are isolated from Nexo.
`GET /api/modules` reports each configured server's enabled/connected state,
discovered and exposed counts, and a safe error code. URLs must be absolute
HTTP(S) URLs; do not put credentials in URLs or logs. MCP tools, like
`web_search`, are sent only to models advertising `tool-calling`.

Troubleshooting: confirm the module is listed in `NEXO_MODULES`, the JSON is
valid, the URL is reachable from the Nexo container, and the server exposes
the configured Streamable HTTP endpoint. A failed server is reported in
`/api/modules` without preventing other modules or SearXNG from starting.

### Agent runtime foundation (Etapa 2.5)

Tool execution runs through a contextual agent runtime. Tool handlers receive
the conversation, provider, model, and tool-round context; tools are exposed
only when the selected model advertises `tool-calling`. The runtime allows at
most three tool rounds, limits serialized tool output, isolates tool errors,
and logs the tool, round, status, and duration without logging arguments or
results by default. Existing SQLite data and chat contracts require no
migration.

See `docs/adr/0001-fundamento-modular.md` for the Etapa 1 decisions and
`tests/test_kernel.py` for the contract checks.

### Decision Runtime (Etapa 3.5)

`decision-runtime` is an optional typed decision capability. It is separate
from `AgentRuntime` (`REASON`) and `ToolRegistry`/MCP (`ACT`); it does not
route chats, choose tools, select models, or change existing chat behavior.

Enable it with an explicit module list and configure its Arbiter adapter:

```text
NEXO_MODULES=attachments,decision-runtime
NEXO_DECISION_PROVIDER=arbiter
NEXO_ARBITER_URL=http://arbiter:8000
NEXO_ARBITER_API_KEY=replace-me
NEXO_DECISION_MODEL=jev-latest
NEXO_DECISION_TIMEOUT=10
NEXO_DECISION_SHADOW=false
```

The experimental `POST /api/decisions` endpoint accepts `state` and a list of
typed questions (`boolean`, `choice`, `score`). The adapter sends the
System-One-compatible `POST <NEXO_ARBITER_URL>/v1/systemone` request,
translating boolean questions to `noul`; choice and score probabilities and
confidence are preserved in the normalized response. The exact Arbiter URL is
configurable; no Laya or Jev package is installed in Nexo.

`jev-latest` is the default model ID. `NEXO_DECISION_MODEL` can override it;
Nexo forwards the value to Arbiter without interpreting checkpoint names.
Arbiter readiness uses authenticated `GET /readyz` (not `/health`), and an
unavailable response only marks the optional module unavailable. The System One
boolean primitive is intentionally named `noul`: `P(true)` is `noul` and
`P(false)` is `1 - noul`.

When the module is absent there is no runtime, provider, request, or decision
route. When enabled, an unavailable Arbiter is reported by `GET /api/modules`
and does not block Nexo startup. Decision failures are isolated and returned as
safe typed errors; API keys and decision payloads are not logged or included in
module diagnostics. See `docs/adr/0003-runtime-opcional-de-decisiones.md`.

Set `NEXO_DECISION_SHADOW=true` to run the three-question Laya/Arbiter decision
(`needs_web`, `needs_tools`, `task_type`) beside normal chat execution. Shadow
results are observational only and are stored locally in SQLite. Inspect recent
records with `GET /api/lab/shadow`; the Lab exposes the same read-only view.
Shadow defaults to `false`, and setting it without enabling `decision-runtime`
records an unavailable observation without affecting chat.

## Next milestones

1. Etapa 2: optional `web-search-searxng` tool, with timeout/error handling,
   visible sources, and availability gated by model tool-calling capability.
2. Etapa 2.5: agent runtime foundation with contextual tool execution,
   explicit capability gating, bounded rounds, bounded output, and isolated
   errors.
3. Etapa 3.5: optional typed Decision Runtime foundation with Arbiter adapter.
4. Etapa 3B: MCP administration, provenance and approvals.
5. Etapa 4: optional Souls module for per-conversation profiles and tool
   allowlists, without changing existing chats.
6. Etapa 5: independent RAG libraries module with ingestion states, separate
   embedding configuration, retrieval and document-linked citations.
7. User authentication and per-user data isolation before exposing the service
   beyond a trusted LAN/reverse proxy. Provider API keys are stored in SQLite;
   keep the data volume private and back it up securely.

## Portainer

For Portainer, deploy `docker-compose.portainer.yml` as a stack from the
repository. Its `${VARIABLE:-default}` values allow the stack environment to
override configuration without editing YAML. Set these variables when using
the optional modules:

```text
NEXO_MODULES=attachments,web-search-searxng,mcp,decision-runtime
NEXO_SEARXNG_URL=http://searxng:8111
NEXO_ARBITER_URL=https://laya.example.com
NEXO_ARBITER_API_KEY=replace-me
NEXO_DECISION_MODEL=jev-latest
```

`NEXO_ARBITER_API_KEY` is optional only when Arbiter does not require
authentication. Keep it as a Portainer secret where possible. After changing
stack variables, save and redeploy/recreate the stack; changing the form alone
does not change an already-running container. Verify `GET /api/modules` and
keep the `nexo-data` volume. For oMLX or other remote services, configure an
address reachable from the Nexo container; `localhost` points to the container
itself.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8787
```
