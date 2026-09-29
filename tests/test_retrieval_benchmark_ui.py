import asyncio
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient
from app.migrations import migrate
from app.notebooks import NotebookInput, NotebookRepository, NotebookService

from app.benchmark import RetrievalBenchmarkService, SCENARIOS


class RetrievalBenchmarkUiTests(unittest.TestCase):
    def test_bounds_and_matrix_validation(self):
        for repetitions, limits in ((0, [20]), (21, [20]), (1, []), (1, [1, 1]), (1, [201]), (1, list(range(1, 12)))):
            with self.assertRaises(ValueError):
                RetrievalBenchmarkService.validate(repetitions, limits)
        RetrievalBenchmarkService.validate(20, [20, 5])

    def test_async_job_lifecycle_progress_and_single_live_job(self):
        async def exercise():
            service = RetrievalBenchmarkService()
            entered, release = asyncio.Event(), asyncio.Event()

            async def run(repetitions, candidate_limits, notebook_name, progress=None):
                progress({"candidate_limit": candidate_limits[0], "scenario": SCENARIOS[0][0], "completed": 0})
                entered.set()
                await release.wait()
                return {"mode": "live", "experiments": []}

            with patch("app.benchmark._run_live", run):
                started = await service.start(1, [20], "Notebook")
                await entered.wait()
                self.assertEqual(started["status"], "running")
                self.assertEqual(service.get(started["id"])["progress"]["scenario"], SCENARIOS[0][0])
                with self.assertRaises(RuntimeError):
                    await service.start(1, [10], "Notebook")
                release.set()
                for _ in range(20):
                    await asyncio.sleep(0)
                    result = service.get(started["id"])
                    if result["status"] != "running":
                        break
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["result"], {"mode": "live", "experiments": []})
                self.assertIsNone(service.active_job)
        asyncio.run(exercise())

    def test_failed_job_serializes_only_safe_failure(self):
        async def exercise():
            service = RetrievalBenchmarkService()

            async def fail(*args, **kwargs):
                raise RuntimeError("provider secret=do-not-return")

            with patch("app.benchmark._run_live", fail):
                job = await service.start(1, [20], "Notebook")
                for _ in range(20):
                    await asyncio.sleep(0)
                    job = service.get(job["id"])
                    if job["status"] != "running":
                        break
                self.assertEqual(job["status"], "failed")
                self.assertNotIn("do-not-return", str(job))
        asyncio.run(exercise())

    def test_http_lifecycle_rejection_validation_and_no_persistence(self):
        import app.main as main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark.sqlite3"
            def connection():
                result = sqlite3.connect(path)
                result.row_factory = sqlite3.Row
                result.execute("PRAGMA foreign_keys=ON")
                return result
            with connection() as database:
                migrate(database)
            notebooks = NotebookService(NotebookRepository(connection, lambda: "now"), Path(directory) / "sources")
            notebook = notebooks.create(NotebookInput("Benchmark notebook"))
            old = main.DB_PATH, main.notebooks, main.retrieval_benchmark
            main.DB_PATH, main.notebooks, main.retrieval_benchmark = path, notebooks, RetrievalBenchmarkService()
            async def run(*args, **kwargs):
                await asyncio.sleep(0.15)
                return {"mode": "live", "experiments": []}
            try:
                with patch("app.benchmark._run_live", run), TestClient(main.app) as client:
                    before = connection().execute("SELECT count(*) FROM embedding_configurations").fetchone()[0]
                    payload = {"notebook_id": notebook["id"], "candidate_limits": [20, 10], "repetitions": 1}
                    started = client.post("/api/settings/retrieval-benchmark", json=payload)
                    self.assertEqual(started.status_code, 200)
                    job_id = started.json()["id"]
                    self.assertEqual(client.post("/api/settings/retrieval-benchmark", json=payload).status_code, 409)
                    self.assertEqual(client.post("/api/settings/retrieval-benchmark", json={**payload, "candidate_limits": [20, 20]}).status_code, 400)
                    self.assertEqual(client.get(f"/api/settings/retrieval-benchmark/{job_id}").status_code, 200)
                    for _ in range(30):
                        status = client.get(f"/api/settings/retrieval-benchmark/{job_id}").json()
                        if status["status"] != "running":
                            break
                        import time
                        time.sleep(0.02)
                    self.assertEqual(status["status"], "completed")
                    self.assertEqual(status["result"]["mode"], "live")
                    after = connection().execute("SELECT count(*) FROM embedding_configurations").fetchone()[0]
                    self.assertEqual(before, after)
            finally:
                main.DB_PATH, main.notebooks, main.retrieval_benchmark = old


if __name__ == "__main__":
    unittest.main()
