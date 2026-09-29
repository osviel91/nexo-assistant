import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.vector_store import LocalVectorStore, QdrantVectorStore, VectorRecord, VectorSearchFilters


class FakeIndex:
    def search_candidates(self, notebook_id, vector, identity):
        return [SimpleNamespace(chunk_id="local", score=1.0)]

    def records(self, document_id):
        return [VectorRecord("local", [1.0], "n", "s", document_id)]


class FakeQdrantClient:
    def __init__(self, **kwargs):
        self.points = {}

    def collection_exists(self, collection):
        return bool(self.points)

    def create_collection(self, collection, vectors_config):
        return None

    def upsert(self, collection, points, wait=True):
        self.points.update({point.id: point for point in points})

    def query_points(self, collection, query, query_filter, limit):
        return SimpleNamespace(points=[SimpleNamespace(id=point.id, score=1.0) for point in list(self.points.values())[:limit]])

    def delete(self, collection, points_selector, wait=True):
        self.points.clear()

    def count(self, collection, count_filter, exact=True):
        return SimpleNamespace(count=len(self.points))

    def get_collections(self):
        return []


class VectorStoreContractTests(unittest.TestCase):
    def test_local_backend_obeys_store_shape(self):
        store = LocalVectorStore(FakeIndex())
        record = VectorRecord("local", [1.0], "n", "s", "d")
        self.assertEqual(store.upsert([record]).count, 1)
        self.assertEqual(store.search([1.0], VectorSearchFilters(notebook_id="n"), 1)[0].chunk_id, "local")
        self.assertEqual(store.health().backend, "local")

    def test_qdrant_backend_is_disposable_and_hydrates_matches(self):
        models = types.ModuleType("qdrant_client.models")
        models.Distance = SimpleNamespace(COSINE="cosine")
        models.Filter = lambda must: must
        models.FieldCondition = lambda key, match: (key, match)
        models.MatchValue = lambda value: value
        models.PointStruct = lambda id, vector, payload: SimpleNamespace(id=id, vector=vector, payload=payload)
        models.VectorParams = lambda size, distance: (size, distance)
        client_module = types.ModuleType("qdrant_client")
        client_module.QdrantClient = FakeQdrantClient
        with patch.dict(sys.modules, {"qdrant_client": client_module, "qdrant_client.models": models}):
            store = QdrantVectorStore("http://qdrant", "chunks", lambda matches, filters: matches)
            record = VectorRecord("q", [1.0, 0.0], "n", "s", "d", {"provider_id": "p"})
            self.assertEqual(store.upsert([record]).count, 1)
            self.assertEqual(store.search([1.0, 0.0], VectorSearchFilters(notebook_id="n"), 1)[0][0], "q")
            self.assertEqual(store.count(VectorSearchFilters()), 1)
            self.assertEqual(store.health().backend, "qdrant")
            store.delete_notebook("n")
            self.assertEqual(store.count(VectorSearchFilters()), 0)


if __name__ == "__main__":
    unittest.main()
