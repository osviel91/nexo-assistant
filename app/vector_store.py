from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence


@dataclass(frozen=True)
class VectorRecord:
    chunk_id: str
    vector: Sequence[float]
    notebook_id: str
    source_id: str
    document_id: str
    identity: dict[str, Any] | None = None


@dataclass(frozen=True)
class VectorSearchFilters:
    notebook_id: str | None = None
    source_id: str | None = None
    document_id: str | None = None
    identity: dict[str, Any] | None = None


@dataclass(frozen=True)
class VectorSearchResult:
    chunk_id: str
    score: float


@dataclass(frozen=True)
class VectorWriteResult:
    count: int


@dataclass(frozen=True)
class VectorStoreHealth:
    status: str
    backend: str
    detail: str | None = None


class VectorStore(Protocol):
    def upsert(self, records: Sequence[VectorRecord]) -> VectorWriteResult: ...
    def search(self, query_vector: Sequence[float], filters: VectorSearchFilters, limit: int) -> Sequence[VectorSearchResult]: ...
    def delete_source(self, source_id: str) -> None: ...
    def delete_notebook(self, notebook_id: str) -> None: ...
    def count(self, filters: VectorSearchFilters) -> int: ...
    def health(self) -> VectorStoreHealth: ...


class LocalVectorStore:
    def __init__(self, index: Any) -> None:
        self.index = index

    def upsert(self, records: Sequence[VectorRecord]) -> VectorWriteResult:
        return VectorWriteResult(len(records))

    def search(self, query_vector: Sequence[float], filters: VectorSearchFilters, limit: int) -> Sequence[Any]:
        return self.index.search_candidates(filters.notebook_id, list(query_vector), filters.identity)[:limit]

    def delete_source(self, source_id: str) -> None:
        return None

    def delete_notebook(self, notebook_id: str) -> None:
        return None

    def count(self, filters: VectorSearchFilters) -> int:
        if filters.document_id:
            return len(self.index.records(filters.document_id))
        return 0

    def health(self) -> VectorStoreHealth:
        return VectorStoreHealth("ok", "local")


class QdrantVectorStore:
    """Disposable dense index. SQLite remains responsible for result hydration."""

    def __init__(self, url: str, collection: str, result_loader: Callable[[Sequence[tuple[str, float]], VectorSearchFilters], Sequence[Any]], api_key: str = "") -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, Filter, FieldCondition, MatchValue, PointStruct, VectorParams
        except ImportError as error:
            raise RuntimeError("qdrant-client is required when NEXO_VECTOR_STORE=qdrant") from error
        self._qdrant = QdrantClient(url=url, api_key=api_key or None)
        self._models = Distance, Filter, FieldCondition, MatchValue, PointStruct, VectorParams
        self.collection = collection
        self.result_loader = result_loader
        self.dimension: int | None = None

    def _ensure_collection(self, dimension: int) -> None:
        if self._qdrant.collection_exists(self.collection):
            return
        Distance, _, _, _, _, VectorParams = self._models
        self._qdrant.create_collection(self.collection, vectors_config=VectorParams(size=dimension, distance=Distance.COSINE))

    def _filter(self, filters: VectorSearchFilters):
        _, Filter, FieldCondition, MatchValue, _, _ = self._models
        conditions = []
        for key in ("notebook_id", "source_id", "document_id"):
            value = getattr(filters, key)
            if value is not None:
                conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
        if filters.identity:
            for key in ("provider_id", "model_id", "embedding_config_version", "chunking_config_hash", "document_hash"):
                if key in filters.identity and key != "dimension" and (key != "document_hash" or filters.identity[key]):
                    conditions.append(FieldCondition(key=key, match=MatchValue(value=filters.identity[key])))
        return Filter(must=conditions) if conditions else None

    def upsert(self, records: Sequence[VectorRecord]) -> VectorWriteResult:
        if not records:
            return VectorWriteResult(0)
        dimension = len(records[0].vector)
        if not dimension or any(len(record.vector) != dimension for record in records):
            raise ValueError("vectors must have one non-zero dimension")
        self._ensure_collection(dimension)
        _, _, _, _, PointStruct, _ = self._models
        points = [PointStruct(id=record.chunk_id, vector=list(record.vector), payload={
            "notebook_id": record.notebook_id, "source_id": record.source_id, "document_id": record.document_id,
            **(record.identity or {}),
        }) for record in records]
        self._qdrant.upsert(self.collection, points=points, wait=True)
        self.dimension = dimension
        return VectorWriteResult(len(points))

    def search(self, query_vector: Sequence[float], filters: VectorSearchFilters, limit: int) -> Sequence[VectorSearchResult]:
        if not query_vector or limit <= 0 or not self._qdrant.collection_exists(self.collection):
            return []
        response = self._qdrant.query_points(self.collection, query=list(query_vector), query_filter=self._filter(filters), limit=limit)
        matches = [(str(point.id), float(point.score)) for point in response.points]
        return self.result_loader(matches, filters)[:limit]

    def delete_source(self, source_id: str) -> None:
        if self._qdrant.collection_exists(self.collection):
            _, Filter, FieldCondition, MatchValue, _, _ = self._models
            self._qdrant.delete(self.collection, points_selector=Filter(must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))]), wait=True)

    def delete_notebook(self, notebook_id: str) -> None:
        if self._qdrant.collection_exists(self.collection):
            _, Filter, FieldCondition, MatchValue, _, _ = self._models
            self._qdrant.delete(self.collection, points_selector=Filter(must=[FieldCondition(key="notebook_id", match=MatchValue(value=notebook_id))]), wait=True)

    def count(self, filters: VectorSearchFilters) -> int:
        if not self._qdrant.collection_exists(self.collection):
            return 0
        return int(self._qdrant.count(self.collection, count_filter=self._filter(filters), exact=True).count)

    def health(self) -> VectorStoreHealth:
        try:
            self._qdrant.get_collections()
            return VectorStoreHealth("ok", "qdrant")
        except Exception as error:
            return VectorStoreHealth("unavailable", "qdrant", str(error)[:240])
