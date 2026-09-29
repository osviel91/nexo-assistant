import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.chunking import DocumentChunk
from app.embeddings import EmbeddingBatch
from app.grounding import GroundedContext, cited_results
from app.knowledge import EmbeddingConfiguration, KnowledgeConfigurationError, validate_configuration
from app.migrations import migrate
from app.retrieval import RetrievalCandidate, RetrievalService, ReciprocalRankFusion, _deduplicate
from app.vector_index import SQLiteVectorIndex, VectorSearchResult


def candidate(chunk_id, source_id="s", start=0, end=10):
    return RetrievalCandidate(chunk_id, source_id, "d", chunk_id, [], start, end)


class BrokenEmbeddings:
    async def embed(self, texts):
        raise RuntimeError("provider unavailable")


class MurphyEmbeddings:
    async def embed(self, texts):
        return EmbeddingBatch([[1.0, 0.0] for _ in texts], "murphy")


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

    def test_configuration_modes_default_and_round_trip(self):
        base = {"provider_id": "p", "model_id": "m", "target_chunk_size": 10,
                "max_chunk_size": 20, "overlap": 1, "batch_size": 1,
                "retrieval_top_k": 5, "retrieval_max_context_chars": 1000}
        self.assertEqual(validate_configuration(base)["retrieval_mode"], "hybrid")
        for mode in ("lexical", "dense", "hybrid"):
            self.assertEqual(validate_configuration({**base, "retrieval_mode": mode})["retrieval_mode"], mode)

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
        self.assertEqual(service.last_diagnostics["retrieval_mode"], "hybrid")
        self.assertEqual(service.last_diagnostics["configured_retrieval_mode"], "hybrid")
        self.assertEqual(service.last_diagnostics["effective_retrieval_mode"], "lexical")
        self.assertEqual(service.last_diagnostics["dense_failure_reason"], "provider_error")
        self.assertNotIn("vector", service.last_diagnostics)

    def test_murphy_fixture_crosses_fts_retrieval_grounding_and_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "murphy.sqlite3"

            def connection():
                result = sqlite3.connect(path)
                result.row_factory = sqlite3.Row
                result.execute("PRAGMA foreign_keys=ON")
                return result

            with connection() as database:
                migrate(database)
                database.execute("INSERT INTO notebooks VALUES ('n','Sabiduría de Murphy','','now','now')")
                database.execute("INSERT INTO notebook_sources(id,notebook_id,type,title,status,metadata,content_hash,created_at,updated_at,indexing_status) VALUES ('s','n','file','Leyes de Murphy.pdf','ready','{}','doc-hash','now','now','ready')")
                database.execute("INSERT INTO canonical_documents VALUES ('d','n','s','Leyes de Murphy.pdf','LEY DE LA PERVERSIDAD DE LA NATURALEZA.\\nNo se puede determinar a priori en que lado de la tostada hay que poner la mantequilla.','doc-hash','application/pdf',NULL,'{}','now','now')")

            chunk = DocumentChunk("c38", "n", "d", "s", 0,
                                  "LEY DE LA PERVERSIDAD DE LA NATURALEZA. No se puede determinar a priori en que lado de la tostada hay que poner la mantequilla.",
                                  0, 126, 22, {"page": 38, "provenance": [{"source_type": "pdf", "source_location": {"page": 38}}]}, "chunk-hash")
            SQLiteVectorIndex(connection, lambda: "now").upsert([chunk], [[1.0, 0.0]])
            with connection() as database:
                counts = database.execute("SELECT (SELECT COUNT(*) FROM document_chunks), (SELECT COUNT(*) FROM document_chunks_fts)").fetchone()
                self.assertEqual(tuple(counts), (1, 1))
                for match in ("perversidad", "naturaleza", '"perversidad de la naturaleza"'):
                    row = database.execute("SELECT chunk_id, notebook_id, source_id FROM document_chunks_fts WHERE document_chunks_fts MATCH ?", (match,)).fetchone()
                    self.assertEqual(tuple(row), ("c38", "n", "s"))

            service = RetrievalService(object(), SQLiteVectorIndex(connection, lambda: "now"), MurphyEmbeddings())
            results = asyncio.run(service.search("n", "¿Cuál es la ley de perversidad de la naturaleza?", 5))
            self.assertGreater(service.last_diagnostics["dense_candidate_count"], 0)
            self.assertGreater(service.last_diagnostics["lexical_candidate_count"], 0)
            self.assertTrue(service.last_diagnostics["fts_available"])
            self.assertEqual(service.last_diagnostics["retrieval_mode"], "hybrid")
            self.assertGreater(service.last_diagnostics["final_candidate_count"], 0)
            context = GroundedContext.build("n", "¿Cuál es la ley de perversidad de la naturaleza?", results, 1000)
            self.assertGreater(context.context_chars, 0)
            self.assertTrue(cited_results("La respuesta es [S1]", context))
            self.assertIn("LEY DE LA PERVERSIDAD DE LA NATURALEZA", context.retrieval_results[0]["content"])
            self.assertEqual(context.retrieval_results[0]["provenance"][0]["source_location"]["page"], 38)


if __name__ == "__main__":
    unittest.main()
