# UX-1 validation

## Automated smoke

- `.venv/bin/python -m pytest -q`: 38 passed.
- `.venv/bin/python -m compileall -q app`: passed.
- `node --check web/assets/app.js`: passed.
- `git diff --check`: passed.
- `GET /api/health`: returned `{"status":"ok","version":"0.1.0"}`.
- `GET /api/modules`: returned the active module catalog.
- `NEXO_MODULES=`: returned an empty module catalog and did not expose attachment UI by configuration.
- `GET /`: served the new shell with the grouped model selector.

## Responsive smoke matrix

The CSS defines mobile behavior at `max-width: 700px`, uses `100dvh`, safe-area insets, a drawer/backdrop, bottom-sheet surfaces, and an internal scroll boundary for messages. Target viewports:

`320x568`, `375x667`, `390x844`, `430x932`, `768x1024`, `1440x900`.

The repository has no browser automation dependency or installed Chromium/Playwright runtime. A graphical Safari capture was attempted in the current environment but the display session could not create an image. Visual screenshots remain a manual follow-up when a graphical browser session is available.

## Preserved flows

- New chat, conversation list, conversation loading and client-side chat search.
- Provider create, edit, delete, model refresh and model deletion.
- SSE chat streaming and source URL validation.
- Attachment upload/removal, gated by the `attachments` module.
- Lab cards gated by `decision-runtime`, `mcp`, and `web-search-searxng`.
