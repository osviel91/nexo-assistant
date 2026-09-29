import asyncio
import unittest

from app.knowledge import EmbeddingConfiguration, KnowledgeConfigurationError, validate_configuration
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
    def test_configuration_serializes_stage9a_defaults(self):
        values = {"provider_id": "p", "model_id": "m", "target_chunk_size": 10,
                  "max_chunk_size": 20, "overlap": 1, "batch_size": 1,
                  "retrieval_top_k": 5, "retrieval_max_context_chars": 1000}
        configuration = validate_configuration(values)
        self.assertEqual({key: configuration[key] for key in ("retrieval_mode", "dense_candidate_limit", "lexical_candidate_limit", "rrf_k", "final_top_k")},
                         {"retrieval_mode": "hybrid", "dense_candidate_limit": 20, "lexical_candidate_limit": 20, "rrf_k": 60, "final_top_k": 5})

    def test_configuration_round_trips_stage9a_values(self):
        values = {"provider_id": "p", "model_id": "m", "target_chunk_size": 10,
                  "max_chunk_size": 20, "overlap": 1, "batch_size": 1,
                  "retrieval_top_k": 9, "retrieval_max_context_chars": 1000,
                  "retrieval_mode": "lexical", "dense_candidate_limit": 31,
                  "lexical_candidate_limit": 17, "rrf_k": 77, "final_top_k": 6}
        configuration = EmbeddingConfiguration(id="c", config_version=1, created_at="now", updated_at="now", **values)
        serialized = configuration.public()
        self.assertEqual({key: validate_configuration({key: serialized[key] for key in values})[key] for key in values}, values)

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
