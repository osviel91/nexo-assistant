# Stage 7C: Chunking, embeddings and retrieval

Stage 7C stops at a developer retrieval API. It does not add retrieval to chat,
`AgentRuntime`, prompts, reranking, or answer generation.

## Contracts

`DocumentChunk` contains stable `id`, `notebook_id`, `document_id`, `source_id`,
deterministic `ordinal`, content, canonical offsets, an honest whitespace token
estimate, structured metadata, and a SHA-256 content hash. Provenance is kept as
structured canonical span intersections, not citation strings.

`ChunkingStrategy` is currently the structure-aware `chunk_document` function.
Markdown headings, PDF pages, and paragraph boundaries are considered before
word-limit splitting. `target_tokens`, `max_tokens`, and `overlap_tokens` are
explicit configuration. Without a model tokenizer, counts are labelled
`whitespace_estimate`.

`EmbeddingProvider` is an async protocol returning `EmbeddingBatch`. The
OpenAI-compatible adapter calls `POST /embeddings`, supports configurable base
URL, API key, model, timeout, and batch size, and does not persist credentials.

`VectorIndex` exposes `upsert`, `delete_document`, and notebook-scoped `search`.
The implementation is `SQLiteVectorIndex`: vectors are JSON in SQLite and are
ranked with cosine similarity in Python. This is intentionally simple for the
existing SQLite-first deployment. A larger vector service can replace this
boundary if measured corpus size or latency requires it.

## Lifecycle and API

Index state is independent from extraction: `not_indexed`, `indexing`, `ready`,
or `failed`. Indexing requires a ready canonical document. Embeddings are fully
computed and validated before the SQLite replacement transaction. Re-indexing
replaces one document's chunks atomically; unchanged document hashes skip the
provider. Failed indexing marks the source unavailable for search, so old
vectors cannot leak after canonical content changes.

Endpoints:

- `POST /api/notebooks/{notebook_id}/sources/{source_id}/index`
- `POST /api/notebooks/{notebook_id}/retrieve` with `{ "query": "...", "limit": 5 }`

Retrieval returns chunk ID, source/document IDs, score, content, canonical range,
and structured provenance. Notebook ownership is enforced before embedding and
the SQLite query filters both notebook ownership and `indexing_status='ready'`.

Operational measurements are retained on the source status: chunk count,
embedding batches, embedding duration, indexing duration, and indexed time.
Runtime Trace is not used.
