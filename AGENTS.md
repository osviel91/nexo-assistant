# Nexo Chat Agent Guide

## Structure

- `app/main.py` is the entire FastAPI backend, including SQLite schema/startup, API routes, provider calls, file extraction, and SSE chat streaming.
- `web/index.html`, `web/assets/app.js`, and `web/assets/app.css` are the served frontend; there is no frontend build step.
- The application entrypoint is `app.main:app`. Docker runs Uvicorn on port `8787`.

## Development

- Local setup and run: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && uvicorn app.main:app --reload --host 0.0.0.0 --port 8787`.
- Container verification/deployment uses `docker compose up -d --build`; preserve the named `nexo-data` volume so SQLite data is not lost.
- There are no configured tests, lint, formatter, typecheck, CI, or codegen commands. At minimum, run `python -m compileall app` after backend edits and exercise `GET /api/health` when the app is running.

## Runtime Constraints

- SQLite is stored at `$NEXO_DATA_DIR/nexo.sqlite3`; the default is `./data`. Do not commit `data/`, SQLite files, or provider API keys.
- `NEXO_MAX_UPLOAD_MB` controls the upload limit and defaults to `15`; Compose sets it to `15` explicitly.
- Providers must expose an OpenAI-compatible API base URL, normally ending in `/v1`; model discovery calls `GET /models` and chat calls `/chat/completions`.
- In Docker, provider URLs must be reachable from the container. `localhost` refers to the Nexo container, not the host or an inference machine.
- API keys stay server-side in SQLite and are intentionally not returned to the browser; preserve that boundary when changing provider or chat APIs.
