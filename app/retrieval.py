from __future__ import annotations

import time
import json
import math
from typing import Any

from app.chunking import ChunkingConfig, chunk_document
from app.embeddings import EmbeddingError, EmbeddingProvider, validate_batch
from app.vector_index import SQLiteVectorIndex, VectorIndex
from app.knowledge import EmbeddingConfiguration


class RetrievalError(ValueError):
    pass


class RetrievalService:
    def __init__(self, repository: Any, index: VectorIndex, provider: EmbeddingProvider,
                 config: ChunkingConfig | None = None, batch_size: int = 32,
                 embedding_config: EmbeddingConfiguration | None = None,
                 provider_id: str | None = None, model_id: str | None = None) -> None:
        self.repository, self.index, self.provider = repository, index, provider
        self.config, self.batch_size = config or ChunkingConfig(), max(1, batch_size)
        self.embedding_config, self.provider_id, self.model_id = embedding_config, provider_id, model_id

    async def index_source(self, notebook_id: str, source_id: str) -> dict[str, Any]:
        source = self.repository.source(notebook_id, source_id)
        if source is None:
            raise LookupError(source_id)
        document = self.repository.canonical(notebook_id, source_id)
        if document is None or source["status"] != "ready":
            raise RetrievalError("canonical document is not ready")
        with self.repository.connection_factory() as connection:
            existing_hash = connection.execute("SELECT metadata FROM document_chunks WHERE document_id=? LIMIT 1", (document["id"],)).fetchone()
            identity = connection.execute("SELECT * FROM vector_index_identities WHERE document_id=?", (document["id"],)).fetchone()
        current_identity = self._identity(document)
        reusable = bool(existing_hash and json.loads(existing_hash[0] or "{}").get("document_content_hash") == document["content_hash"])
        if self.embedding_config:
            reusable = reusable and identity is not None and all(identity[key] == value for key, value in current_identity.items())
        if reusable:
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
            if self.embedding_config and vectors:
                current_identity["dimension"] = len(vectors[0])
            indexed = self.index.upsert(chunks, vectors, current_identity if self.embedding_config else None)
            indexing_duration = round((time.monotonic() - started) * 1000, 2)
            self.repository.set_indexing_status(source_id, "ready", chunk_count=indexed, embedding_batches=batches,
                                                embedding_duration_ms=embedding_duration, indexing_duration_ms=indexing_duration,
                                                embedding_count=len(vectors), vector_count=indexed, embedding_dimension=len(vectors[0]) if vectors else None,
                                                embedding_config_version=self.embedding_config.config_version if self.embedding_config else None,
                                                indexing_identity=json.dumps(current_identity, sort_keys=True))
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
        identity = self._identity({"content_hash": ""}) if self.embedding_config else None
        return [{"chunk_id": item.chunk_id, "source_id": item.source_id, "document_id": item.document_id,
                  "score": round(item.score, 6), "content": item.content, "canonical_start": item.canonical_start,
                  "canonical_end": item.canonical_end, "provenance": item.provenance,
                  "document_content_hash": item.document_content_hash, "chunk_content_hash": item.chunk_content_hash,
                  "source_title": item.source_title}
                 for item in self.index.search(notebook_id, batch.vectors[0], limit, identity)]

    def status(self, source_id: str) -> dict[str, Any]:
        with self.repository.connection_factory() as connection:
            row = connection.execute("""SELECT indexing_status,indexing_error,chunk_count,embedding_batches,
                embedding_duration_ms,indexing_duration_ms,indexed_at FROM notebook_sources WHERE id=?""", (source_id,)).fetchone()
        return dict(row) if row else {"indexing_status": "not_indexed"}

    def _identity(self, document: dict[str, Any]) -> dict[str, Any]:
        if not self.embedding_config:
            return {}
        return {"provider_id": self.provider_id, "model_id": self.model_id,
                "dimension": 0, "embedding_config_version": self.embedding_config.config_version,
                "chunking_config_hash": self.embedding_config.chunking_hash(), "document_hash": document["content_hash"]}

    def integrity(self, notebook_id: str, source_id: str) -> dict[str, Any]:
        document = self.repository.canonical(notebook_id, source_id)
        checks = []
        if not document:
            return {"status": "FAILED", "checks": [{"name": "DOCUMENT", "status": "FAIL", "detail": "canonical document missing"}]}
        with self.repository.connection_factory() as connection:
            identity = connection.execute("SELECT * FROM vector_index_identities WHERE document_id=?", (document["id"],)).fetchone()
            chunks = connection.execute("SELECT * FROM document_chunks WHERE document_id=? ORDER BY ordinal", (document["id"],)).fetchall()
            orphans = connection.execute("SELECT COUNT(*) FROM document_chunks dc LEFT JOIN canonical_documents cd ON cd.id=dc.document_id WHERE cd.id IS NULL").fetchone()[0]
        expected = len(chunks)
        checks.append({"name": "DOCUMENT_HASH", "status": "PASS" if identity and identity["document_hash"] == document["content_hash"] else "FAIL", "detail": document["content_hash"]})
        checks.append({"name": "CHUNK_COUNT", "status": "PASS", "detail": expected})
        vectors = [json.loads(row["embedding"]) for row in chunks if row["embedding"]]
        checks.append({"name": "VECTOR_COUNT", "status": "PASS" if len(vectors) == expected else "FAIL", "detail": f"{len(vectors)}/{expected}"})
        dimensions = {len(vector) for vector in vectors}
        dimension = identity["dimension"] if identity else None
        checks.append({"name": "DIMENSION", "status": "PASS" if identity and len(dimensions) == 1 and next(iter(dimensions), None) == dimension else "FAIL", "detail": dimension})
        finite = all(isinstance(value, (int, float)) and math.isfinite(value) for vector in vectors for value in vector)
        checks.append({"name": "FINITE_VALUES", "status": "PASS" if finite else "FAIL"})
        checks.append({"name": "ORPHANS", "status": "PASS" if not orphans else "FAIL", "detail": orphans})
        compatible = bool(identity and self.embedding_config and identity["provider_id"] == self.provider_id and identity["model_id"] == self.model_id and identity["embedding_config_version"] == self.embedding_config.config_version and identity["chunking_config_hash"] == self.embedding_config.chunking_hash())
        checks.append({"name": "EMBEDDING_IDENTITY", "status": "PASS" if compatible else "FAIL"})
        return {"status": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL", "checks": checks, "identity": dict(identity) if identity else None}

    def _chunk_count(self, document_id: str) -> int:
        with self.repository.connection_factory() as connection:
            return connection.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=?", (document_id,)).fetchone()[0]
