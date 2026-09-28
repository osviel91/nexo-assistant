from __future__ import annotations

import time
import json
from typing import Any

from app.chunking import ChunkingConfig, chunk_document
from app.embeddings import EmbeddingError, EmbeddingProvider, validate_batch
from app.vector_index import SQLiteVectorIndex, VectorIndex


class RetrievalError(ValueError):
    pass


class RetrievalService:
    def __init__(self, repository: Any, index: VectorIndex, provider: EmbeddingProvider,
                 config: ChunkingConfig | None = None, batch_size: int = 32) -> None:
        self.repository, self.index, self.provider = repository, index, provider
        self.config, self.batch_size = config or ChunkingConfig(), max(1, batch_size)

    async def index_source(self, notebook_id: str, source_id: str) -> dict[str, Any]:
        source = self.repository.source(notebook_id, source_id)
        if source is None:
            raise LookupError(source_id)
        document = self.repository.canonical(notebook_id, source_id)
        if document is None or source["status"] != "ready":
            raise RetrievalError("canonical document is not ready")
        with self.repository.connection_factory() as connection:
            existing_hash = connection.execute("SELECT metadata FROM document_chunks WHERE document_id=? LIMIT 1",
                                               (document["id"],)).fetchone()
        if existing_hash and json.loads(existing_hash[0] or "{}").get("document_content_hash") == document["content_hash"]:
            self.repository.set_indexing_status(source_id, "ready", chunk_count=self._chunk_count(document["id"]))
            return self.status(source_id) | {"skipped": True}
        self.repository.set_indexing_status(source_id, "indexing")
        started = time.monotonic()
        chunks = chunk_document(document, self.config)
        vectors: list[list[float]] = []
        batches = 0
        embedding_started = time.monotonic()
        try:
            for offset in range(0, len(chunks), self.batch_size):
                batch = await self.provider.embed([chunk.content for chunk in chunks[offset:offset + self.batch_size]])
                validate_batch(batch, len(chunks[offset:offset + self.batch_size]))
                vectors.extend(batch.vectors)
                batches += 1
            embedding_duration = round((time.monotonic() - embedding_started) * 1000, 2)
            indexed = self.index.upsert(chunks, vectors)
            indexing_duration = round((time.monotonic() - started) * 1000, 2)
            self.repository.set_indexing_status(source_id, "ready", chunk_count=indexed, embedding_batches=batches,
                                                embedding_duration_ms=embedding_duration, indexing_duration_ms=indexing_duration)
            return self.status(source_id) | {"skipped": False}
        except Exception as error:
            self.repository.set_indexing_status(source_id, "failed", error=str(error)[:240],
                                                embedding_batches=batches,
                                                embedding_duration_ms=round((time.monotonic() - embedding_started) * 1000, 2))
            raise EmbeddingError("indexing failed") from error

    async def search(self, notebook_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        if not query.strip():
            raise RetrievalError("query must not be empty")
        limit = min(max(limit, 1), 50)
        batch = await self.provider.embed([query])
        validate_batch(batch, 1)
        return [{"chunk_id": item.chunk_id, "source_id": item.source_id, "document_id": item.document_id,
                 "score": round(item.score, 6), "content": item.content, "canonical_start": item.canonical_start,
                 "canonical_end": item.canonical_end, "provenance": item.provenance}
                for item in self.index.search(notebook_id, batch.vectors[0], limit)]

    def status(self, source_id: str) -> dict[str, Any]:
        with self.repository.connection_factory() as connection:
            row = connection.execute("""SELECT indexing_status,indexing_error,chunk_count,embedding_batches,
                embedding_duration_ms,indexing_duration_ms,indexed_at FROM notebook_sources WHERE id=?""", (source_id,)).fetchone()
        return dict(row) if row else {"indexing_status": "not_indexed"}

    def _chunk_count(self, document_id: str) -> int:
        with self.repository.connection_factory() as connection:
            return connection.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=?", (document_id,)).fetchone()[0]
