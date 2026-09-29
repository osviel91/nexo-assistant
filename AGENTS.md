# Nexo Chat Agent Guide

## Structure

- `app/main.py` is the entire FastAPI backend, including SQLite schema/startup, API routes, provider calls, file extraction, and SSE chat streaming.
- `web/index.html`, `web/assets/app.js`, and `web/assets/app.css` are the served frontend; there is no frontend build step.
- The application entrypoint is `app.main:app`. Docker runs Uvicorn on port `8787`.

## Development

- Canonical setup and commands: `./scripts/nexo bootstrap`, `./scripts/nexo dev`, and `./scripts/nexo check`. See `docs/development.md` and `docs/coding-agent.md`.
- Container verification/deployment uses `docker compose up -d --build`; preserve the named `nexo-data` volume so SQLite data is not lost.
- The quality gate is `./scripts/nexo check`; it runs the pinned Python suite, `compileall`, frontend regressions, and hygiene checks. Use `./scripts/nexo smoke` for a local health endpoint check.

## Runtime Constraints

- SQLite is stored at `$NEXO_DATA_DIR/nexo.sqlite3`; the default is `./data`. Do not commit `data/`, SQLite files, or provider API keys.
- `NEXO_MAX_UPLOAD_MB` controls the upload limit and defaults to `15`; Compose sets it to `15` explicitly.
- Providers must expose an OpenAI-compatible API base URL, normally ending in `/v1`; model discovery calls `GET /models` and chat calls `/chat/completions`.
- In Docker, provider URLs must be reachable from the container. `localhost` refers to the Nexo container, not the host or an inference machine.
- API keys stay server-side in SQLite and are intentionally not returned to the browser; preserve that boundary when changing provider or chat APIs.
