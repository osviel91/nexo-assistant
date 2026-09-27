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

## Next milestones

1. Etapa 2: optional `web-search-searxng` tool, with timeout/error handling,
   visible sources, and availability gated by model tool-calling capability.
2. Etapa 2.5: agent runtime foundation with contextual tool execution,
   explicit capability gating, bounded rounds, bounded output, and isolated
   errors.
3. Etapa 3: optional MCP module, starting with one container-safe transport,
   admin allowlists, provenance, approvals and output/time limits.
4. Etapa 4: optional Souls module for per-conversation profiles and tool
   allowlists, without changing existing chats.
5. Etapa 5: independent RAG libraries module with ingestion states, separate
   embedding configuration, retrieval and document-linked citations.
6. User authentication and per-user data isolation before exposing the service
   beyond a trusted LAN/reverse proxy. Provider API keys are stored in SQLite;
   keep the data volume private and back it up securely.

## Portainer

Deploy `docker-compose.yml` as a stack. Map port 8787 as desired and keep the `nexo-data` volume. For oMLX, configure the base URL using an address the container can reach; `localhost` inside the container refers to the container itself.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8787
```
