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


class VectorIndex(Protocol):
    def upsert(self, chunks: list[DocumentChunk], vectors: list[list[float]]) -> int: ...
    def delete_document(self, document_id: str) -> None: ...
    def search(self, notebook_id: str, vector: list[float], limit: int) -> list[VectorSearchResult]: ...


def _cosine(left: list[float], right: list[float]) -> float:
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0


class SQLiteVectorIndex:
    def __init__(self, connection_factory, now) -> None:
        self.connection_factory, self.now = connection_factory, now

    def upsert(self, chunks: list[DocumentChunk], vectors: list[list[float]]) -> int:
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
        return len(chunks)

    def delete_document(self, document_id: str) -> None:
        with self.connection_factory() as connection:
            connection.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))

    def search(self, notebook_id: str, vector: list[float], limit: int) -> list[VectorSearchResult]:
        if not vector or limit <= 0:
            return []
        with self.connection_factory() as connection:
            rows = connection.execute("""SELECT document_chunks.* FROM document_chunks
                                      JOIN notebook_sources ON notebook_sources.id=document_chunks.source_id
                                      WHERE document_chunks.notebook_id=? AND document_chunks.embedding IS NOT NULL
                                      AND notebook_sources.indexing_status='ready'""",
                                      (notebook_id,)).fetchall()
        results = []
        for row in rows:
            candidate = json.loads(row["embedding"])
            if len(candidate) != len(vector):
                continue
            metadata = json.loads(row["metadata"] or "{}")
            results.append(VectorSearchResult(row["id"], row["notebook_id"], row["source_id"], row["document_id"],
                                              _cosine(vector, candidate), row["content"], row["canonical_start"],
                                              row["canonical_end"], metadata.get("provenance", [])))
        return sorted(results, key=lambda item: (-item.score, item.chunk_id))[:limit]
