import asyncio
import unittest
import httpx
from types import SimpleNamespace

from app.grounding import GroundedContext, cited_results
from app.embeddings import EmbeddingBatch
from app.knowledge import EmbeddingConfiguration, validate_configuration
from app.retrieval import RetrievalCandidate, RetrievalService
from app.reranking import OpenAICompatibleReranker, RerankerError
from app.retrieval_evaluation import EvaluationCase, RetrievalEvaluationHarness, mrr, ndcg_at_k, precision_at_k, recall_at_k
from app.vector_index import VectorSearchResult


class Embeddings:
    async def embed(self, texts):
        return EmbeddingBatch([[1.0]], "embedding")


class Index:
    def search_candidates(self, notebook_id, vector, identity):
        return [VectorSearchResult("page-6", notebook_id, "murphy", "doc", .99, "unrelated page 6", 0, 10, []),
                VectorSearchResult("page-38", notebook_id, "murphy", "doc", .98, "LEY DE LA PERVERSIDAD DE LA NATURALEZA. No se puede determinar a priori en que lado de la tostada hay que poner la mantequilla.", 10, 100, [])]


class Reranker:
    provider_id, model_id = "rerank", "murphy-reranker"

    async def rerank(self, query, candidates, limit):
        for rank, candidate in enumerate(reversed(candidates), 1):
            candidate.rerank_score = 1 / rank
        return list(reversed(candidates))


class FailingReranker(Reranker):
    async def rerank(self, query, candidates, limit):
        raise RuntimeError("secret provider response")


def configuration(**overrides):
    values = {"provider_id": "p", "model_id": "m", "target_chunk_size": 10, "max_chunk_size": 20,
              "overlap": 1, "batch_size": 1, "retrieval_top_k": 1, "retrieval_max_context_chars": 1000,
              "retrieval_mode": "dense", "dense_candidate_limit": 20, "lexical_candidate_limit": 20,
              "rrf_k": 60, "final_top_k": 1, **overrides}
    return EmbeddingConfiguration(id="c", config_version=1, created_at="now", updated_at="now", **validate_configuration(values))


class Stage9BRerankingTests(unittest.TestCase):
    def service(self, config, reranker=None):
        return RetrievalService(object(), Index(), Embeddings(), embedding_config=config, provider_id="p", model_id="m", reranker=reranker)

    def test_disabled_keeps_rrf_order(self):
        service = self.service(configuration())
        result = asyncio.run(service.search("n", "tostada", 1))
        self.assertEqual(result[0]["chunk_id"], "page-6")
        self.assertEqual(service.last_diagnostics["reranker_status"], "disabled")

    def test_reranker_reorders_and_preserves_evidence(self):
        service = self.service(configuration(reranking_enabled=True, reranker_provider_id="rerank", reranker_model="murphy-reranker"), Reranker())
        result = asyncio.run(service.search("n", "tostada", 1))
        self.assertEqual(result[0]["chunk_id"], "page-38")
        self.assertEqual(result[0]["rrf_rank"], 2)
        self.assertEqual(result[0]["rerank_rank"], 1)
        self.assertEqual(service.last_diagnostics["reranker_status"], "applied")

    def test_provider_failure_falls_back_without_secret(self):
        service = self.service(configuration(reranking_enabled=True, reranker_provider_id="rerank", reranker_model="murphy-reranker"), FailingReranker())
        result = asyncio.run(service.search("n", "tostada", 1))
        self.assertEqual(result[0]["chunk_id"], "page-6")
        self.assertEqual(service.last_diagnostics["reranker_reason"], "provider_error")
        self.assertNotIn("secret", str(service.last_diagnostics))

    def test_ood_gate_rejects_candidates_regardless_of_reranker(self):
        conversation = [{"role": "user", "content": "¿Cuál es la ley de perversidad de la naturaleza?"},
                        {"role": "assistant", "content": "La tostada."}]
        configurations = (
            (configuration(), None, "disabled"),
            (configuration(reranking_enabled=True, reranker_provider_id="rerank", reranker_model="murphy-reranker"), Reranker(), "applied"),
            (configuration(reranking_enabled=True, reranker_provider_id="rerank", reranker_model="murphy-reranker"), FailingReranker(), "fallback"),
        )
        for config, reranker, rerank_status in configurations:
            with self.subTest(rerank_status=rerank_status):
                config = configuration(relevance_gate_enabled=True, **{key: value for key, value in {
                    "reranking_enabled": config.reranking_enabled,
                    "reranker_provider_id": config.reranker_provider_id,
                    "reranker_model": config.reranker_model,
                }.items() if value or key == "reranking_enabled"})
                service = self.service(config, reranker)
                results = asyncio.run(service.search("n", "¿Qué dice el documento sobre Kubernetes?", 2, conversation))
                self.assertGreater(len(results), 0)
                self.assertEqual(service.last_diagnostics["relevant_candidate_count"], 0)
                self.assertEqual(service.last_diagnostics["relevance_gate_status"], "rejected")
                self.assertEqual(service.last_diagnostics["relevance_gate_reason"], "insufficient_evidence")
                self.assertTrue(all(not item["relevant"] for item in results))

    def test_direct_and_conversational_queries_keep_murphy_evidence(self):
        service = self.service(configuration(final_top_k=2))
        direct = asyncio.run(service.search("n", "¿Cuál es la ley de perversidad de la naturaleza?", 2))
        self.assertGreater(len(direct), 0)
        self.assertGreater(service.last_diagnostics["relevant_candidate_count"], 0)
        self.assertEqual(service.last_diagnostics["relevance_gate_status"], "accepted")
        direct_context = GroundedContext.build("n", "direct", [item for item in direct if item["relevant"]], 1000)
        self.assertIn("PERVERSIDAD", direct_context.retrieval_results[0]["content"])

        follow_up = asyncio.run(service.search(
            "n", "¿Y cuál es su corolario sobre la tostada?", 2,
            [{"role": "user", "content": "¿Cuál es la ley de perversidad de la naturaleza?"}],
        ))
        self.assertGreater(len(follow_up), 0)
        self.assertGreater(service.last_diagnostics["relevant_candidate_count"], 0)
        self.assertEqual(service.last_diagnostics["relevance_gate_status"], "accepted")
        follow_up_context = GroundedContext.build("n", "follow-up", [item for item in follow_up if item["relevant"]], 1000)
        self.assertTrue(any("tostada" in item["content"] for item in follow_up_context.retrieval_results))

        ood = asyncio.run(service.search(
            "n", "¿Qué dice el documento sobre Kubernetes?", 2,
            [{"role": "user", "content": "¿Cuál es la ley de perversidad de la naturaleza?"},
             {"role": "assistant", "content": "La tostada cae [S1]."}],
        ))
        self.assertGreater(len(ood), 0)
        self.assertEqual(service.last_diagnostics["relevant_candidate_count"], 0)
        self.assertEqual(service.last_diagnostics["relevance_gate_status"], "rejected")
        self.assertEqual(cited_results("No hay evidencia [S1]", GroundedContext.build("n", "ood", [], 1000)), [])

    def test_metrics_and_citation_mapping_are_structural(self):
        results = [{"chunk_id": "page-38", "source_id": "murphy", "page_number": 38}, {"chunk_id": "page-6", "source_id": "murphy", "page_number": 6}]
        relevant = {"chunk_ids": ["page-38"], "source_ids": [], "page_numbers": [38]}
        self.assertEqual(recall_at_k(results, relevant, 1), 1.0)
        self.assertEqual(precision_at_k(results, relevant, 1), 1.0)
        self.assertEqual(mrr(results, relevant), 1.0)
        self.assertEqual(ndcg_at_k(results, relevant, 1), 1.0)
        context = GroundedContext.build("n", "q", [{"chunk_id": "page-6", "source_id": "s", "document_id": "d", "content": "wrong"}, {"chunk_id": "page-38", "source_id": "s", "document_id": "d", "content": "correct"}], 100)
        self.assertEqual(cited_results("claim [S2]", context)[0][1]["content"], "correct")

    def test_baseline_harness_uses_same_cases(self):
        cases = [EvaluationCase("q", "n", {"chunk_ids": ["page-38"], "source_ids": [], "page_numbers": []})]
        report = asyncio.run(RetrievalEvaluationHarness({"hybrid": lambda n, q: [{"chunk_id": "page-38"}], "hybrid+rerank": lambda n, q: [{"chunk_id": "page-38"}]}, cases).run(1))
        self.assertEqual(report["hybrid"]["recall_at_k"], report["hybrid+rerank"]["recall_at_k"])

    def test_reranker_protocol_success_and_malformed_failure_are_safe(self):
        def success(request):
            return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(success))
        try:
            ranked = asyncio.run(OpenAICompatibleReranker(client, "http://provider", {}, "p", "m").rerank("q", [SimpleNamespace(text="a", rerank_score=None), SimpleNamespace(text="b", rerank_score=None)], 2))
            self.assertEqual([item.text for item in ranked], ["b", "a"])
            self.assertEqual(ranked[0].rerank_score, 0.9)
        finally:
            asyncio.run(client.aclose())

        async def malformed():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))) as malformed_client:
                await OpenAICompatibleReranker(malformed_client, "http://provider", {}, "p", "m").rerank("q", [SimpleNamespace(text="a")], 1)

        with self.assertRaises(RerankerError):
            asyncio.run(malformed())


if __name__ == "__main__":
    unittest.main()
