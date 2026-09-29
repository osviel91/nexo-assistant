import asyncio
import unittest

from app.knowledge import KnowledgeConfigurationError, validate_configuration
from app.retrieval import RetrievalCandidate, RetrievalService, ReciprocalRankFusion, _deduplicate
from app.vector_index import VectorSearchResult


def candidate(chunk_id, source_id="s", start=0, end=10):
    return RetrievalCandidate(chunk_id, source_id, "d", chunk_id, [], start, end)


class BrokenEmbeddings:
    async def embed(self, texts):
        raise RuntimeError("provider unavailable")


class LexicalIndex:
    def lexical_search(self, notebook_id, query, limit):
        return [VectorSearchResult("lexical", notebook_id, "s", "d", 1.0, "rare term", 0, 9, [])]


class Stage9ARetrievalTests(unittest.TestCase):
    def test_rrf_is_deterministic_and_rewards_overlap(self):
        both = candidate("both")
        lexical = candidate("lexical")
        dense = candidate("dense")
        dense.dense_rank, dense.dense_score = 1, .9
        both.dense_rank, both.dense_score = 2, .8
        both.lexical_rank, both.lexical_score = 1, -1
        lexical.lexical_rank, lexical.lexical_score = 2, -2
        result = ReciprocalRankFusion().fuse([[dense, both], [both, lexical]], 60)
        self.assertEqual([item.chunk_id for item in result], ["both", "dense", "lexical"])
        self.assertEqual(result[0].dense_rank, 2)
        self.assertEqual(result[0].lexical_rank, 1)

    def test_overlap_deduplication_is_conservative(self):
        kept = candidate("one", start=0, end=100)
        duplicate = candidate("two", start=10, end=90)
        separate = candidate("three", start=200, end=300)
        self.assertEqual([item.chunk_id for item in _deduplicate([kept, duplicate, separate])], ["one", "three"])

    def test_configuration_rejects_unknown_mode(self):
        values = {"provider_id": "p", "model_id": "m", "target_chunk_size": 10,
                  "max_chunk_size": 20, "overlap": 1, "batch_size": 1,
                  "retrieval_top_k": 5, "retrieval_max_context_chars": 1000,
                  "retrieval_mode": "invalid"}
        with self.assertRaises(KnowledgeConfigurationError):
            validate_configuration(values)

    def test_hybrid_uses_lexical_when_dense_fails(self):
        service = RetrievalService(object(), LexicalIndex(), BrokenEmbeddings())
        result = asyncio.run(service.search("n", "rare term", 5))
        self.assertEqual(result[0]["chunk_id"], "lexical")
        self.assertEqual(service.last_diagnostics["retrieval_mode"], "lexical")
        self.assertNotIn("vector", service.last_diagnostics)


if __name__ == "__main__":
    unittest.main()
