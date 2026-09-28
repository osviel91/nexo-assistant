import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ingestion import MarkdownAdapter, NotebookIngestionService, PdfAdapter, WebAdapter, validate_public_url
from app.migrations import migrate
from app.notebooks import NotebookInput, NotebookRepository, NotebookService


class FakePage:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class FakeReader:
    def __init__(self, _stream):
        self.pages = [FakePage("one"), FakePage("two")]


class Stage7BIngestionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.path, self.storage = root / "nexo.sqlite3", root / "sources"

        def connection():
            result = sqlite3.connect(self.path)
            result.row_factory = sqlite3.Row
            result.execute("PRAGMA foreign_keys=ON")
            return result

        self.connection = connection
        with connection() as database:
            migrate(database)
        self.repository = NotebookRepository(connection, lambda: "now")
        self.notebooks = NotebookService(self.repository, self.storage)
        self.ingestion = NotebookIngestionService(self.repository, self.storage, lambda: "now")
        self.notebook = self.notebooks.create(NotebookInput("Knowledge"))

    def tearDown(self):
        self.directory.cleanup()

    def test_text_and_markdown_preserve_content_and_headings(self):
        text = self.notebooks.add_file(self.notebook["id"], "Notes", "notes.txt", "text/plain", b"line one\nline two")
        self.ingestion.ingest(self.notebook["id"], text["id"])
        document = self.notebooks.canonical(self.notebook["id"], text["id"])
        self.assertEqual(document["content"], "line one\nline two")
        markdown = self.notebooks.add_file(self.notebook["id"], "Guide", "guide.md", "text/markdown", b"# Intro\n\nBody")
        self.ingestion.ingest(self.notebook["id"], markdown["id"])
        spans = self.notebooks.canonical(self.notebook["id"], markdown["id"])["spans"]
        self.assertTrue(any(span["source_location"].get("heading") == "Intro" for span in spans))

    def test_pdf_pages_are_provenance_spans(self):
        source = self.notebooks.add_file(self.notebook["id"], "Book", "book.pdf", "application/pdf", b"fixture")
        with patch("app.ingestion.PdfReader", FakeReader):
            self.ingestion.ingest(self.notebook["id"], source["id"])
        document = self.notebooks.canonical(self.notebook["id"], source["id"])
        self.assertEqual([span["source_location"]["page"] for span in document["spans"]], [1, 2])
        self.assertEqual(document["content"], "one\n\ntwo")

    def test_empty_pdf_and_malformed_pdf_fail_safely(self):
        source = self.notebooks.add_file(self.notebook["id"], "Empty", "empty.pdf", "application/pdf", b"fixture")
        with patch("app.ingestion.PdfReader", lambda _: type("Reader", (), {"pages": [FakePage("")]})()):
            result = self.ingestion.ingest(self.notebook["id"], source["id"])
        self.assertEqual(result["error_code"], "EMPTY_PDF_EXTRACTION")
        malformed = self.notebooks.add_file(self.notebook["id"], "Bad", "bad.pdf", "application/pdf", b"not a pdf")
        self.ingestion.ingest(self.notebook["id"], malformed["id"])
        self.assertEqual(self.notebooks.source(self.notebook["id"], malformed["id"])["error_code"], "MALFORMED_PDF")

    def test_web_html_extraction_is_deterministic_and_keeps_url(self):
        source = self.notebooks.add_web(self.notebook["id"], "Page", "https://example.com/docs")
        adapter = WebAdapter(lambda url: (url, b"<nav>noise</nav><h1>Title</h1><p>Useful text</p><script>bad()</script>", "text/html"))
        service = NotebookIngestionService(self.repository, self.storage, lambda: "now", adapters=[adapter])
        service.ingest(self.notebook["id"], source["id"])
        document = self.notebooks.canonical(self.notebook["id"], source["id"])
        self.assertEqual(document["content"], "Title\n\nUseful text")
        self.assertEqual(document["metadata"]["url"], "https://example.com/docs")

    def test_failure_retry_reingestion_and_unchanged_document_id(self):
        source = self.notebooks.add_file(self.notebook["id"], "Retry", "retry.txt", "text/plain", b"first")
        self.ingestion.ingest(self.notebook["id"], source["id"])
        first = self.notebooks.canonical(self.notebook["id"], source["id"])
        self.ingestion.ingest(self.notebook["id"], source["id"])
        second = self.notebooks.canonical(self.notebook["id"], source["id"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["content_hash"], second["content_hash"])
        bad = self.notebooks.add_file(self.notebook["id"], "Invalid", "invalid.txt", "text/plain", b"\xff")
        self.ingestion.ingest(self.notebook["id"], bad["id"])
        self.assertEqual(self.notebooks.source(self.notebook["id"], bad["id"])["status"], "failed")
        path = self.storage / bad["id"][:2] / bad["id"]
        path.write_bytes(b"fixed")
        self.ingestion.ingest(self.notebook["id"], bad["id"])
        self.assertEqual(self.notebooks.source(self.notebook["id"], bad["id"])["status"], "ready")

    def test_cascade_and_cross_notebook_isolation(self):
        other = self.notebooks.create(NotebookInput("Other"))
        source = self.notebooks.add_file(self.notebook["id"], "Private", "private.txt", "text/plain", b"secret")
        self.ingestion.ingest(self.notebook["id"], source["id"])
        with self.assertRaises(LookupError):
            self.notebooks.canonical(other["id"], source["id"])
        self.notebooks.delete(self.notebook["id"])
        with self.connection() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM canonical_documents").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM canonical_spans").fetchone()[0], 0)

    def test_ssrf_rejects_private_and_non_http_targets(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1", "http://localhost"):
            with self.assertRaises(Exception):
                validate_public_url(url)
        with patch("app.ingestion.socket.getaddrinfo", return_value=[(None, None, None, None, ("192.168.1.8", 80))]):
            with self.assertRaises(Exception):
                validate_public_url("https://example.com")


if __name__ == "__main__":
    unittest.main()
