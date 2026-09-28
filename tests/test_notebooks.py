import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.migrations import migrate
from app.notebooks import NotebookInput, NotebookRepository, NotebookService, NotebookValidationError


class NotebookTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nexo.sqlite3"
        self.storage = Path(self.directory.name) / "notebook-sources"

        def connection():
            result = sqlite3.connect(self.path)
            result.row_factory = sqlite3.Row
            result.execute("PRAGMA foreign_keys=ON")
            return result

        self.connection = connection
        with connection() as database:
            migrate(database)
        self.service = NotebookService(NotebookRepository(connection, lambda: "now"), self.storage)

    def tearDown(self):
        self.directory.cleanup()

    def test_notebook_and_source_lifecycle(self):
        notebook = self.service.create(NotebookInput("Architecture", "Notes"))
        source = self.service.add_file(notebook["id"], "Architecture PDF", "architecture.pdf", "application/pdf", b"pdf bytes")
        web = self.service.add_web(notebook["id"], "Docs", "https://example.com/docs")
        self.assertEqual(len(self.service.sources(notebook["id"])), 2)
        self.assertEqual(self.service.source(notebook["id"], source["id"])["content_hash"], source["content_hash"])
        self.service.delete_source(notebook["id"], web["id"])
        self.service.delete(notebook["id"])
        self.assertEqual(list(self.storage.rglob("*")), [])

    def test_validation_and_isolation(self):
        with self.assertRaises(NotebookValidationError):
            self.service.create(NotebookInput(""))
        first = self.service.create(NotebookInput("First"))
        second = self.service.create(NotebookInput("Second"))
        self.service.add_web(first["id"], "Source", "https://example.com")
        self.assertEqual(len(self.service.sources(second["id"])), 0)
        with self.assertRaises(NotebookValidationError):
            self.service.add_web(first["id"], "Bad", "file:///etc/passwd")
        with self.assertRaises(NotebookValidationError):
            self.service.add_file(first["id"], "Bad", "../../secret.txt", "text/plain", b"x")

    def test_api_crud_and_upload_limit(self):
        import app.main as main
        old_path, old_service = main.DB_PATH, main.notebooks
        main.DB_PATH, main.notebooks = self.path, self.service
        try:
            old_limit = main.MAX_UPLOAD
            with TestClient(main.app) as client:
                notebook = client.post("/api/notebooks", json={"name": "API notebook"}).json()
                self.assertEqual(client.get("/api/notebooks").status_code, 200)
                self.assertEqual(client.patch(f"/api/notebooks/{notebook['id']}", json={"name": "Renamed"}).json()["name"], "Renamed")
                main.MAX_UPLOAD = 4
                self.assertEqual(client.post(f"/api/notebooks/{notebook['id']}/sources", files={"file": ("large.txt", b"hello", "text/plain")}).status_code, 413)
                main.MAX_UPLOAD = old_limit
                uploaded = client.post(f"/api/notebooks/{notebook['id']}/sources", files={"file": ("note.md", b"hello", "text/markdown")})
                self.assertEqual(uploaded.status_code, 200)
                source_id = uploaded.json()["id"]
                self.assertEqual(client.get(f"/api/notebooks/{notebook['id']}/sources/{source_id}").status_code, 200)
                self.assertEqual(client.delete(f"/api/notebooks/{notebook['id']}/sources/{source_id}").status_code, 200)
                self.assertEqual(client.delete(f"/api/notebooks/{notebook['id']}").status_code, 200)
        finally:
            main.MAX_UPLOAD = old_limit
            main.DB_PATH, main.notebooks = old_path, old_service


if __name__ == "__main__":
    unittest.main()
