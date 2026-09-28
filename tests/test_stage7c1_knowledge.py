import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.embeddings import EmbeddingBatch, EmbeddingError
from app.ingestion import NotebookIngestionService
from app.knowledge import EmbeddingConfiguration, KnowledgeConfigurationError, validate_configuration
from app.migrations import migrate
from app.notebooks import NotebookInput, NotebookRepository, NotebookService
from app.retrieval import RetrievalService
from app.vector_index import SQLiteVectorIndex


class Embeddings:
    async def embed(self, texts):
        return EmbeddingBatch([[float(len(text)), 1.0] for text in texts], "model-a")


class CountingEmbeddings(Embeddings):
    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        self.calls.append(texts)
        return await super().embed(texts)


class FailingEmbeddings(Embeddings):
    async def embed(self, texts):
        raise RuntimeError("provider unavailable")


class Stage7C1KnowledgeTests(unittest.TestCase):
    def test_configuration_is_bounded_and_chunking_semantics_are_explicit(self):
        with self.assertRaises(KnowledgeConfigurationError):
            validate_configuration({"provider_id": "p", "model_id": "m", "target_chunk_size": 9, "max_chunk_size": 8, "overlap": 1, "batch_size": 1, "retrieval_top_k": 1, "retrieval_max_context_chars": 1000})

    def test_index_identity_is_persisted_and_incompatible_query_returns_no_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            def connection():
                result = sqlite3.connect(path)
                result.row_factory = sqlite3.Row
                result.execute("PRAGMA foreign_keys=ON")
                return result
            with connection() as database:
                migrate(database)
            repository = NotebookRepository(connection, lambda: "now")
            notebooks = NotebookService(repository, Path(directory) / "sources")
            ingestion = NotebookIngestionService(repository, Path(directory) / "sources", lambda: "now")
            notebook = notebooks.create(NotebookInput("Knowledge"))
            source = notebooks.add_file(notebook["id"], "Notes", "notes.txt", "text/plain", b"one two three")
            ingestion.ingest(notebook["id"], source["id"])
            config = EmbeddingConfiguration("c", "provider-a", "model-a", 2, 3, 0, 2, 5, 12000, 1, "now", "now")
            service = RetrievalService(repository, SQLiteVectorIndex(connection, lambda: "now"), Embeddings(),
                                       embedding_config=config, provider_id="provider-a", model_id="model-a")
            asyncio.run(service.index_source(notebook["id"], source["id"]))
            with connection() as database:
                identity = database.execute("SELECT provider_id,model_id,dimension FROM vector_index_identities").fetchone()
            self.assertEqual(tuple(identity), ("provider-a", "model-a", 2))
            incompatible = EmbeddingConfiguration("c", "provider-b", "model-b", 2, 3, 0, 2, 5, 12000, 2, "now", "now")
            rejected = RetrievalService(repository, SQLiteVectorIndex(connection, lambda: "now"), Embeddings(),
                                        embedding_config=incompatible, provider_id="provider-b", model_id="model-b")
            self.assertEqual(asyncio.run(rejected.search(notebook["id"], "one")), [])

    def test_legacy_index_is_repaired_and_current_index_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            def connection():
                result = sqlite3.connect(path)
                result.row_factory = sqlite3.Row
                result.execute("PRAGMA foreign_keys=ON")
                return result
            with connection() as database:
                migrate(database)
            repository = NotebookRepository(connection, lambda: "now")
            notebooks = NotebookService(repository, Path(directory) / "sources")
            ingestion = NotebookIngestionService(repository, Path(directory) / "sources", lambda: "now")
            provider = CountingEmbeddings()
            notebook = notebooks.create(NotebookInput("Knowledge"))
            source = notebooks.add_file(notebook["id"], "Notes", "notes.txt", "text/plain", b"Murphy problems happen at the worst moment")
            ingestion.ingest(notebook["id"], source["id"])
            index = SQLiteVectorIndex(connection, lambda: "now")
            legacy = RetrievalService(repository, index, provider)
            asyncio.run(legacy.index_source(notebook["id"], source["id"]))

            config = EmbeddingConfiguration("c", "provider-a", "model-a", 20, 30, 0, 2, 5, 12000, 1, "now", "now")
            current = RetrievalService(repository, index, provider, embedding_config=config,
                                       provider_id="provider-a", model_id="model-a")
            self.assertEqual(asyncio.run(current.search(notebook["id"], "worst moment")), [])
            repaired = asyncio.run(current.index_source(notebook["id"], source["id"]))
            self.assertFalse(repaired["skipped"])
            self.assertTrue(asyncio.run(current.search(notebook["id"], "worst moment")))
            calls = len(provider.calls)
            reused = asyncio.run(current.index_source(notebook["id"], source["id"]))
            self.assertTrue(reused["skipped"])
            self.assertEqual(len(provider.calls), calls)

    def test_indexing_success_and_failure_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            def connection():
                result = sqlite3.connect(path)
                result.row_factory = sqlite3.Row
                result.execute("PRAGMA foreign_keys=ON")
                return result
            with connection() as database:
                migrate(database)
            repository = NotebookRepository(connection, lambda: "now")
            notebooks = NotebookService(repository, Path(directory) / "sources")
            ingestion = NotebookIngestionService(repository, Path(directory) / "sources", lambda: "now")
            notebook = notebooks.create(NotebookInput("Knowledge"))
            source = notebooks.add_file(notebook["id"], "Notes", "notes.txt", "text/plain", b"one two three")
            ingestion.ingest(notebook["id"], source["id"])
            service = RetrievalService(repository, SQLiteVectorIndex(connection, lambda: "now"), Embeddings())
            result = asyncio.run(service.index_source(notebook["id"], source["id"]))
            self.assertEqual(result["indexing_status"], "ready")
            self.assertEqual(result["chunk_count"], 1)

            failed = notebooks.add_file(notebook["id"], "Other", "other.txt", "text/plain", b"failure")
            ingestion.ingest(notebook["id"], failed["id"])
            failing = RetrievalService(repository, SQLiteVectorIndex(connection, lambda: "now"), FailingEmbeddings())
            with self.assertRaises(EmbeddingError):
                asyncio.run(failing.index_source(notebook["id"], failed["id"]))
            self.assertEqual(notebooks.source(notebook["id"], failed["id"])["indexing_status"], "failed")

    def test_candidate_inspection_explains_identity_rejection_without_vectors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            def connection():
                result = sqlite3.connect(path)
                result.row_factory = sqlite3.Row
                result.execute("PRAGMA foreign_keys=ON")
                return result
            with connection() as database:
                migrate(database)
            repository = NotebookRepository(connection, lambda: "now")
            notebooks = NotebookService(repository, Path(directory) / "sources")
            ingestion = NotebookIngestionService(repository, Path(directory) / "sources", lambda: "now")
            notebook = notebooks.create(NotebookInput("Knowledge"))
            source = notebooks.add_file(notebook["id"], "Notes", "notes.txt", "text/plain", b"Murphy tostada mantequilla")
            ingestion.ingest(notebook["id"], source["id"])
            index = SQLiteVectorIndex(connection, lambda: "now")
            legacy = RetrievalService(repository, index, Embeddings())
            asyncio.run(legacy.index_source(notebook["id"], source["id"]))
            config = EmbeddingConfiguration("c", "provider-a", "model-a", 20, 30, 0, 2, 5, 12000, 1, "now", "now")
            current = RetrievalService(repository, index, Embeddings(), embedding_config=config,
                                       provider_id="provider-a", model_id="model-a")
            candidates = asyncio.run(current.inspect_search(notebook["id"], "tostada", 10))
            self.assertEqual(len(candidates), 1)
            self.assertIn("missing_index_identity", candidates[0]["rejection_reasons"])
            self.assertNotIn("embedding", candidates[0])


if __name__ == "__main__":
    unittest.main()
