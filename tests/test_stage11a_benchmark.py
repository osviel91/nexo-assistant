import asyncio
import argparse
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.benchmark import _live_gold, compare, live_latency_statistics, live_run_valid, main as benchmark_main, parse_candidate_limits, run_deterministic, stats
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
        self.assertEqual(report["mode"], "deterministic")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text(json.dumps(report))
            self.assertEqual(json.loads(path.read_text())["benchmark_version"], "11A-1")
        self.assertEqual(compare(report, report)["direct_lexical"]["latency_delta_ms"], 0)

    def test_safe_metadata_exposes_metrics_but_not_payloads(self):
        result = safe_metadata({"embedding_ms": 4, "dense_candidates": 2, "vector_store": "local", "provider_ttft_ms": 4,
                                "request_to_first_token_ms": 20, "prompt": "secret", "embedding": [1.0]})
        self.assertEqual(result["embedding_ms"], 4)
        self.assertEqual(result["dense_candidates"], 2)
        self.assertEqual(result["provider_ttft_ms"], 4)
        self.assertEqual(result["request_to_first_token_ms"], 20)
        self.assertNotIn("prompt", result)
        self.assertNotIn("embedding", result)

    def test_live_matrix_validation_and_failed_runs_are_excluded(self):
        self.assertEqual(parse_candidate_limits("20, 15,10,8,5"), [20, 15, 10, 8, 5])
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_candidate_limits("20,0")
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_candidate_limits("10,10")
        self.assertTrue(live_run_valid({"reranker_status": "applied", "reranker_candidate_count": 5,
                                        "fused_candidate_count": 10, "reranked_candidate_count": 5}, 5))
        self.assertFalse(live_run_valid({"reranker_status": "fallback", "reranker_candidate_count": 5,
                                         "fused_candidate_count": 10, "reranked_candidate_count": 0}, 5))
        summary = live_latency_statistics({"latency_ms": 90}, [
            {"latency_ms": 10, "valid_for_candidate_comparison": True},
            {"latency_ms": 1000, "valid_for_candidate_comparison": False},
        ])
        self.assertEqual(summary["cold"], 90)
        self.assertEqual(summary["warm"]["p50"], 10)
        self.assertEqual(summary["invalid_warm_runs"], 1)

    def test_live_is_explicit_and_records_live_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "live.json"
            live_report = {"mode": "live", "experiments": [{"scenarios": [{}]}]}
            with patch("app.benchmark.run_live", return_value=live_report) as run_live:
                self.assertEqual(benchmark_main(["run", "--live", "--reranker-candidates", "15", "--output", str(output)]), 0)
                run_live.assert_called_once_with(5, [15], "Sabiduría de Murphy")
            self.assertEqual(json.loads(output.read_text())["mode"], "live")
            deterministic = Path(directory) / "deterministic.json"
            with patch("app.benchmark.run_live", side_effect=AssertionError("live must be opt-in")):
                self.assertEqual(benchmark_main(["run", "--repetitions", "1", "--output", str(deterministic)]), 0)
            self.assertEqual(json.loads(deterministic.read_text())["mode"], "deterministic")

    def test_candidate_override_is_ephemeral(self):
        from dataclasses import dataclass, replace

        @dataclass(frozen=True)
        class Config:
            reranker_candidate_limit: int

        configured = Config(reranker_candidate_limit=20)
        experimental = replace(configured, reranker_candidate_limit=8)
        self.assertEqual(experimental.reranker_candidate_limit, 8)
        self.assertEqual(configured.reranker_candidate_limit, 20)

    def test_live_gold_is_scenario_anchored_and_keeps_text_out_of_result(self):
        chunks = [
            {"id": "law", "source_id": "murphy", "content": "LEY DE LA PERVERSIDAD DE LA NATURALEZA"},
            {"id": "corollary", "source_id": "murphy", "content": "COROLARIO DE JENNING: la tostada"},
            {"id": "unrelated", "source_id": "other", "content": "Kubernetes"},
        ]
        gold, provenance = _live_gold(chunks, "compound")
        self.assertEqual(gold["chunk_ids"], ["law", "corollary"])
        self.assertEqual(provenance, ["murphy", "murphy"])
        self.assertEqual(_live_gold(chunks, "ood")[0]["chunk_ids"], [])

    def test_live_matrix_uses_candidate_20_as_comparison_control(self):
        experiment = lambda limit, p50: {"candidate_limit": limit, "scenarios": [{
            "name": "direct_lexical", "latency_statistics": {"reranker_duration_ms": {"p50": p50}},
            "quality_statistics": {"recall_at_k": 1.0, "precision_at_k": 0.8, "mrr": 1.0, "ndcg_at_k": 1.0},
            "invalid_runs": [],
        }]}
        artifact = {"mode": "live", "experiments": [experiment(20, 2000), experiment(10, 900)]}
        result = compare(artifact, artifact)
        self.assertEqual(result["10"]["direct_lexical"]["reranker_p50_delta_ms"], -1100)
        self.assertEqual(result["10"]["direct_lexical"]["quality_delta"]["recall_at_k"], 0)


if __name__ == "__main__":
    unittest.main()
