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

## Next milestones

1. Document ingestion pipeline and knowledge notebooks (PDF/DOCX/HTML parsing, chunking, embeddings, vector search, citations).
2. User authentication and per-user data isolation before exposing the service beyond a trusted LAN/reverse proxy. Provider API keys are stored in SQLite; keep the data volume private and back it up securely.
3. Provider capability checks and configurable request parameters.

## Portainer

Deploy `docker-compose.yml` as a stack. Map port 8787 as desired and keep the `nexo-data` volume. For oMLX, configure the base URL using an address the container can reach; `localhost` inside the container refers to the container itself.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8787
```
