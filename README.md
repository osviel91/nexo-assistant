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

`GET /api/modules` exposes only active modules and their interface extensions.
Modules declare an id, version, kernel API version, capabilities and optional
dependencies. Lifecycle and chat hooks are finite and failures are isolated
and logged without exposing secrets or blocking the core.

See `docs/adr/0001-fundamento-modular.md` for the Etapa 1 decisions and
`tests/test_kernel.py` for the contract checks.

## Next milestones

1. Etapa 2: optional `web-search-searxng` tool, with timeout/error handling,
   visible sources, and availability gated by model tool-calling capability.
2. Etapa 3: optional MCP module, starting with one container-safe transport,
   admin allowlists, provenance, approvals and output/time limits.
3. Etapa 4: optional Souls module for per-conversation profiles and tool
   allowlists, without changing existing chats.
4. Etapa 5: independent RAG libraries module with ingestion states, separate
   embedding configuration, retrieval and document-linked citations.
5. User authentication and per-user data isolation before exposing the service
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
