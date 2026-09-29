import asyncio
import unittest

from app.grounding import GroundedContext, cited_results
from app.embeddings import EmbeddingBatch
from app.knowledge import EmbeddingConfiguration, validate_configuration
from app.retrieval import RetrievalCandidate, RetrievalService
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


if __name__ == "__main__":
    unittest.main()
