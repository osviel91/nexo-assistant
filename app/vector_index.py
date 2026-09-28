from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from app.chunking import DocumentChunk
from app.embeddings import EmbeddingBatch, validate_batch


@dataclass(frozen=True)
class VectorSearchResult:
    chunk_id: str
    notebook_id: str
    source_id: str
    document_id: str
    score: float
    content: str
    canonical_start: int
    canonical_end: int
    provenance: list[dict]
    document_content_hash: str | None = None
    chunk_content_hash: str | None = None
    source_title: str | None = None


class VectorIndex(Protocol):
    def upsert(self, chunks: list[DocumentChunk], vectors: list[list[float]], identity: dict | None = None) -> int: ...
    def delete_document(self, document_id: str) -> None: ...
    def search(self, notebook_id: str, vector: list[float], limit: int, identity: dict | None = None) -> list[VectorSearchResult]: ...

    def inspect(self, notebook_id: str, vector: list[float], limit: int, identity: dict | None = None) -> list[dict]: ...


def _cosine(left: list[float], right: list[float]) -> float:
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0


class SQLiteVectorIndex:
    def __init__(self, connection_factory, now) -> None:
        self.connection_factory, self.now = connection_factory, now

    def upsert(self, chunks: list[DocumentChunk], vectors: list[list[float]], identity: dict | None = None) -> int:
        if not chunks:
            return 0
        dimension = validate_batch(EmbeddingBatch(vectors), len(chunks))
        document_id = chunks[0].document_id
        if any(chunk.document_id != document_id for chunk in chunks):
            raise ValueError("upsert accepts chunks for one document")
        with self.connection_factory() as connection:
            connection.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
            connection.executemany("""INSERT INTO document_chunks
                (id,notebook_id,document_id,source_id,ordinal,content,canonical_start,canonical_end,
                 token_count,metadata,content_hash,embedding,embedding_dimension,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
                (chunk.id, chunk.notebook_id, chunk.document_id, chunk.source_id, chunk.ordinal, chunk.content,
                 chunk.canonical_start, chunk.canonical_end, chunk.token_count, json.dumps(chunk.metadata),
                 chunk.content_hash, json.dumps(vector), dimension, self.now())
                for chunk, vector in zip(chunks, vectors)
            ])
            if identity:
                connection.execute("""INSERT INTO vector_index_identities
                    (document_id,provider_id,model_id,dimension,embedding_config_version,chunking_config_hash,document_hash,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(document_id) DO UPDATE SET provider_id=excluded.provider_id,model_id=excluded.model_id,
                    dimension=excluded.dimension,embedding_config_version=excluded.embedding_config_version,
                    chunking_config_hash=excluded.chunking_config_hash,document_hash=excluded.document_hash,updated_at=excluded.updated_at""",
                    (document_id, identity["provider_id"], identity["model_id"], dimension, identity["embedding_config_version"],
                     identity["chunking_config_hash"], identity["document_hash"], self.now(), self.now()))
        return len(chunks)

    def delete_document(self, document_id: str) -> None:
        with self.connection_factory() as connection:
            connection.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
            connection.execute("DELETE FROM vector_index_identities WHERE document_id=?", (document_id,))

    def search(self, notebook_id: str, vector: list[float], limit: int, identity: dict | None = None) -> list[VectorSearchResult]:
        if not vector or limit <= 0:
            return []
        with self.connection_factory() as connection:
            query = """SELECT document_chunks.*, notebook_sources.title AS source_title,
                                       canonical_documents.content_hash AS document_content_hash
                                       FROM document_chunks
                                       JOIN notebook_sources ON notebook_sources.id=document_chunks.source_id
                                       JOIN canonical_documents ON canonical_documents.id=document_chunks.document_id
                                       WHERE document_chunks.notebook_id=? AND document_chunks.embedding IS NOT NULL
                                       AND notebook_sources.indexing_status='ready'"""
            params: list = [notebook_id]
            if identity:
                query += " AND EXISTS (SELECT 1 FROM vector_index_identities vii WHERE vii.document_id=document_chunks.document_id AND vii.provider_id=? AND vii.model_id=? AND vii.embedding_config_version=? AND vii.chunking_config_hash=? AND vii.document_hash=canonical_documents.content_hash)"
                params.extend([identity["provider_id"], identity["model_id"], identity["embedding_config_version"], identity["chunking_config_hash"]])
            rows = connection.execute(query, params).fetchall()
        results = []
        for row in rows:
            candidate = json.loads(row["embedding"])
            if len(candidate) != len(vector):
                continue
            metadata = json.loads(row["metadata"] or "{}")
            results.append(VectorSearchResult(row["id"], row["notebook_id"], row["source_id"], row["document_id"],
                                              _cosine(vector, candidate), row["content"], row["canonical_start"],
                                              row["canonical_end"], metadata.get("provenance", []),
                                              row["document_content_hash"], row["content_hash"], row["source_title"]))
        return sorted(results, key=lambda item: (-item.score, item.chunk_id))[:limit]

    def inspect(self, notebook_id: str, vector: list[float], limit: int, identity: dict | None = None) -> list[dict]:
        if not vector or limit <= 0:
            return []
        with self.connection_factory() as connection:
            rows = connection.execute("""SELECT document_chunks.id, document_chunks.source_id,
                    document_chunks.embedding, document_chunks.embedding_dimension,
                    notebook_sources.indexing_status, canonical_documents.content_hash,
                    vii.provider_id, vii.model_id, vii.embedding_config_version,
                    vii.chunking_config_hash, vii.document_hash
                    FROM document_chunks
                    JOIN notebook_sources ON notebook_sources.id=document_chunks.source_id
                    JOIN canonical_documents ON canonical_documents.id=document_chunks.document_id
                    LEFT JOIN vector_index_identities vii ON vii.document_id=document_chunks.document_id
                    WHERE document_chunks.notebook_id=?""", (notebook_id,)).fetchall()
        candidates = []
        for row in rows:
            reasons = []
            score = None
            if row["embedding"] is None:
                reasons.append("missing_embedding")
            else:
                candidate = json.loads(row["embedding"])
                if len(candidate) != len(vector):
                    reasons.append("dimension_mismatch")
                else:
                    score = round(_cosine(vector, candidate), 6)
            if row["indexing_status"] != "ready":
                reasons.append(f"index_{row['indexing_status']}")
            if identity and not row["provider_id"]:
                reasons.append("missing_index_identity")
            elif identity and any(row[key] != identity[key] for key in ("provider_id", "model_id", "embedding_config_version", "chunking_config_hash")):
                reasons.append("embedding_identity_mismatch")
            elif identity and row["document_hash"] != row["content_hash"]:
                reasons.append("document_hash_mismatch")
            candidates.append({"chunk_id": row["id"], "source_id": row["source_id"], "score": score,
                               "accepted": not reasons, "rejection_reasons": reasons})
        candidates.sort(key=lambda item: (item["score"] is None, -(item["score"] or 0), item["chunk_id"]))
        for rank, candidate in enumerate(candidates[:limit], 1):
            candidate["rank"] = rank
        return candidates[:limit]
