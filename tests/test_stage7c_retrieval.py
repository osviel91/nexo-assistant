import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.chunking import ChunkingConfig, chunk_document
from app.embeddings import EmbeddingBatch, EmbeddingError, validate_batch
from app.ingestion import NotebookIngestionService
from app.migrations import migrate
from app.notebooks import NotebookInput, NotebookRepository, NotebookService
from app.retrieval import RetrievalService
from app.vector_index import SQLiteVectorIndex


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        self.calls.append(texts)
        return EmbeddingBatch([[float(len(text)), 1.0] for text in texts], "fake")


class LexicalRecoveryEmbeddings:
    async def embed(self, texts):
        return EmbeddingBatch([[0.0, 1.0] if text.startswith("¿") else [float("tostada" in text), 1.0] for text in texts], "fake")


class Stage7CRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.path, self.storage = root / "nexo.sqlite3", root / "sources"

        def connection():
            result = sqlite3.connect(self.path)
            result.row_factory = sqlite3.Row
            result.execute("PRAGMA foreign_keys=ON")
            return result

        with connection() as database:
            migrate(database)
        self.connection = connection
        self.repository = NotebookRepository(connection, lambda: "now")
        self.notebooks = NotebookService(self.repository, self.storage)
        self.ingestion = NotebookIngestionService(self.repository, self.storage, lambda: "now")
        self.provider = FakeEmbeddings()
        self.retrieval = RetrievalService(self.repository, SQLiteVectorIndex(connection, lambda: "now"), self.provider,
                                          ChunkingConfig(target_tokens=3, max_tokens=4, overlap_tokens=1), batch_size=2)

    def tearDown(self):
        self.directory.cleanup()

    def test_structure_boundaries_ordering_and_provenance(self):
        document = {"id": "d", "notebook_id": "n", "source_id": "s", "source_type": "file",
                    "content": "# One\n\nalpha beta\n\n# Two\n\ngamma", "spans": [
                        {"start_offset": 0, "end_offset": 5, "source_type": "markdown", "source_location": {"heading": "One", "level": 1}},
                        {"start_offset": 19, "end_offset": 24, "source_type": "markdown", "source_location": {"heading": "Two", "level": 1}},
                    ]}
        chunks = chunk_document(document, ChunkingConfig(target_tokens=2, max_tokens=3, overlap_tokens=1))
        self.assertEqual([chunk.ordinal for chunk in chunks], list(range(len(chunks))))
        self.assertTrue(any(chunk.metadata["heading"] == "One" for chunk in chunks))
        self.assertTrue(all(chunk.canonical_end > chunk.canonical_start for chunk in chunks))

    def test_index_retrieval_isolated_and_reindex_unchanged_skips_embeddings(self):
        notebook = self.notebooks.create(NotebookInput("Private"))
        other = self.notebooks.create(NotebookInput("Other"))
        source = self.notebooks.add_file(notebook["id"], "Guide", "guide.md", "text/markdown", b"# Tools\n\nAuthorize tools safely")
        self.ingestion.ingest(notebook["id"], source["id"])
        asyncio.run(self.retrieval.index_source(notebook["id"], source["id"]))
        calls = len(self.provider.calls)
        results = asyncio.run(self.retrieval.search(notebook["id"], "tools", 5))
        self.assertEqual(results[0]["source_id"], source["id"])
        self.assertTrue(results[0]["provenance"])
        self.assertEqual(asyncio.run(self.retrieval.search(other["id"], "tools", 5)), [])
        self.ingestion.ingest(notebook["id"], source["id"])
        asyncio.run(self.retrieval.index_source(notebook["id"], source["id"]))
        self.assertEqual(len(self.provider.calls), calls + 2)  # two query embeddings; re-index was skipped
        self.notebooks.delete(notebook["id"])
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0], 0)

    def test_unavailable_document_and_embedding_validation(self):
        notebook = self.notebooks.create(NotebookInput("Empty"))
        source = self.notebooks.add_web(notebook["id"], "Not indexed", "https://example.com")
        with self.assertRaises(ValueError):
            asyncio.run(self.retrieval.index_source(notebook["id"], source["id"]))
        for batch in (EmbeddingBatch([[float("nan")]]), EmbeddingBatch([[1.0], [1.0, 2.0]])):
            with self.assertRaises(EmbeddingError):
                validate_batch(batch, 1)

    def test_hybrid_retrieval_recovers_lexical_match_below_vector_top_k(self):
        notebook = self.notebooks.create(NotebookInput("Murphy"))
        content = "\n\n".join(["generic material"] * 12 + ["tostada pan mantequilla"])
        source = self.notebooks.add_file(notebook["id"], "Leyes de Murphy.pdf", "murphy.txt", "text/plain", content.encode())
        self.ingestion.ingest(notebook["id"], source["id"])
        retrieval = RetrievalService(self.repository, SQLiteVectorIndex(self.connection, lambda: "now"), LexicalRecoveryEmbeddings(),
                                     ChunkingConfig(target_tokens=5, max_tokens=5, overlap_tokens=0), batch_size=32)
        asyncio.run(retrieval.index_source(notebook["id"], source["id"]))

        diagnostics = asyncio.run(retrieval.inspect_search(notebook["id"], "¿Cuál es la ley de Murphy sobre la tostada?", 20))
        relevant = next(item for item in diagnostics if item["content_matches"]["tostada"])
        self.assertGreater(relevant["rank"], 5)
        self.assertEqual(relevant["candidate_count"], 13)
        self.assertTrue(relevant["has_valid_embedding"])

        results = asyncio.run(retrieval.search(notebook["id"], "¿Cuál es la ley de Murphy sobre la tostada?", 5))
        self.assertTrue(any("tostada" in result["content"] and "mantequilla" in result["content"] for result in results))
        for query in ("tostada", "tostada mantequilla", "pan mantequilla", "ley de Murphy tostada mantequilla"):
            self.assertTrue(any("tostada" in result["content"] for result in asyncio.run(retrieval.search(notebook["id"], query, 5))))


if __name__ == "__main__":
    unittest.main()
