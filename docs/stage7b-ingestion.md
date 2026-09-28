# Stage 7B: Ingestion and Canonical Documents

Stage 7B converts one persistent `NotebookSource` into one `canonical_documents` row. The canonical contract is `id`, `notebook_id`, `source_id`, `title`, UTF-8 `content`, SHA-256 `content_hash`, `content_type`, optional `language`, metadata, and timestamps. No embedding or vector fields exist.

`canonical_spans` stores canonical half-open offsets plus a generic `source_type` and JSON `source_location`. PDF spans contain `page`; Markdown spans contain `heading` and `level`; text spans contain line ranges; web spans contain the final URL. Presentation citation strings are not persisted.

`NotebookIngestionService` resolves adapters, marks the source `extracting`, reads only its generated storage path, validates extraction, replaces the single canonical document and spans transactionally, and marks `ready`. Handled failures mark `failed` with a bounded code and human-readable message. Re-ingestion keeps one document per source and preserves its document id while replacing content and spans.

Adapters are `TextAdapter`, `MarkdownAdapter`, `PdfAdapter`, and `WebAdapter`. Markdown remains Markdown. PDFs use `pypdf` without OCR. Web fetches one HTTP(S) URL, follows at most three redirects, validates every resolved host against loopback/private/link-local/reserved ranges, limits time and size, and accepts only text document MIME types. Credentials are never forwarded.

The explicit API is `POST /api/notebooks/{notebook_id}/sources/{source_id}/ingest`; `GET .../document` reads a canonical document after ownership checks. The existing source API exposes safe lifecycle and developer metadata only. Chat and `AgentRuntime` are unchanged.
