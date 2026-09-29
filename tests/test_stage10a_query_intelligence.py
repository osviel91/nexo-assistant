import asyncio
import unittest

from app.embeddings import EmbeddingBatch
from app.query_intelligence import QueryAnalyzer
from app.retrieval import MultiQueryFusion, RetrievalCandidate, RetrievalService
from app.vector_index import VectorSearchResult


class Embeddings:
    async def embed(self, texts):
        return EmbeddingBatch([[1.0] for _ in texts], "embedding")


class Index:
    def search_candidates(self, notebook_id, vector, identity):
        return [VectorSearchResult("answer", notebook_id, "source", "document", 1.0, "answer", 0, 10, [])]

    def lexical_search(self, notebook_id, query, limit):
        return [VectorSearchResult("answer", notebook_id, "source", "document", 1.0, "answer", 0, 10, [])]


def candidate(chunk_id):
    return RetrievalCandidate(chunk_id, "source", "document", chunk_id, [], 0, 10)


class Stage10AQueryIntelligenceTests(unittest.TestCase):
    def test_simple_query_is_not_expanded(self):
        plan = QueryAnalyzer().analyze("  ley de perversidad  ")
        self.assertEqual(plan.variants, ("ley de perversidad",))
        self.assertEqual(plan.original_query, "ley de perversidad")

    def test_reference_uses_latest_user_context(self):
        plan = QueryAnalyzer().analyze("¿Y eso qué significa?", [
            {"role": "user", "content": "Explícame la ley de perversidad."},
        ])
        self.assertEqual(plan.variants[0], "¿Y eso qué significa?")
        self.assertIn("ley de perversidad", plan.standalone_query)

    def test_compound_query_is_bounded_and_deterministic(self):
        plan = QueryAnalyzer(max_variants=3).analyze("explica la causa y describe el efecto")
        self.assertEqual(plan.variants, ("explica la causa y describe el efecto", "explica la causa", "describe el efecto"))

    def test_multi_query_fusion_keeps_one_canonical_candidate(self):
        result = MultiQueryFusion().fuse([[candidate("a")], [candidate("a"), candidate("b")]])
        self.assertEqual([item.chunk_id for item in result], ["a", "b"])

    def test_retrieval_reports_expansion_without_changing_provenance(self):
        service = RetrievalService(object(), Index(), Embeddings())
        result = asyncio.run(service.search("notebook", "¿Y eso qué significa?", conversation=[
            {"role": "user", "content": "La ley de perversidad."},
        ]))
        self.assertEqual(result[0]["chunk_id"], "answer")
        self.assertTrue(service.last_diagnostics["query_expanded"])
        self.assertGreater(service.last_diagnostics["query_variant_count"], 1)
        self.assertEqual(result[0]["source_id"], "source")


if __name__ == "__main__":
    unittest.main()
