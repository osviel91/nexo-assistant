import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.benchmark import compare, run_deterministic, stats
from app.embeddings import EmbeddingBatch
from app.retrieval import RetrievalService
from app.runtime_trace import safe_metadata
from app.vector_index import VectorSearchResult


class Embeddings:
    async def embed(self, texts):
        return EmbeddingBatch([[1.0]], "test")


class Index:
    def search_candidates(self, notebook_id, vector, identity):
        return [VectorSearchResult("law", notebook_id, "s", "d", 1.0, "ley perversidad naturaleza tostada", 0, 20, [])]

    def lexical_search(self, notebook_id, query, limit, identity=None):
        return self.search_candidates(notebook_id, [], identity)


class Stage11ABenchmarkTests(unittest.TestCase):
    def test_retrieval_telemetry_populates_and_unexecuted_is_none(self):
        service = RetrievalService(object(), Index(), Embeddings())
        asyncio.run(service.search("n", "ley de perversidad", 1))
        telemetry = service.last_telemetry
        self.assertIsNotNone(telemetry.query_analysis_ms)
        self.assertIsNotNone(telemetry.embedding_ms)
        self.assertIsNotNone(telemetry.dense_search_ms)
        self.assertIsNotNone(telemetry.lexical_search_ms)
        self.assertIsNone(telemetry.reranking_ms)
        self.assertEqual(telemetry.dense_search_count, 1)
        self.assertEqual(telemetry.lexical_search_count, 1)
        self.assertEqual(telemetry.dense_candidates, 1)
        self.assertEqual(telemetry.retrieved_candidates, 1)

    def test_stats_and_comparison_are_stdlib_and_stable(self):
        self.assertEqual(stats([1, 2, 3, 4]), {"count": 4, "min": 1, "median": 2.5, "p50": 2.5, "p95": 3, "max": 4, "mean": 2.5})
        report = run_deterministic(1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text(json.dumps(report))
            self.assertEqual(json.loads(path.read_text())["benchmark_version"], "11A-1")
        self.assertEqual(compare(report, report)["direct_lexical"]["latency_delta_ms"], 0)

    def test_safe_metadata_exposes_metrics_but_not_payloads(self):
        result = safe_metadata({"embedding_ms": 4, "dense_candidates": 2, "vector_store": "local", "prompt": "secret", "embedding": [1.0]})
        self.assertEqual(result["embedding_ms"], 4)
        self.assertEqual(result["dense_candidates"], 2)
        self.assertNotIn("prompt", result)
        self.assertNotIn("embedding", result)


if __name__ == "__main__":
    unittest.main()
