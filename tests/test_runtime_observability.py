import json
import tempfile
import unittest
from pathlib import Path


class RuntimeObservabilityTests(unittest.TestCase):
    def setUp(self):
        from app import main

        self.main = main
        self.directory = tempfile.TemporaryDirectory()
        self.old_db = main.DB_PATH
        main.DB_PATH = Path(self.directory.name) / "runtime.sqlite3"
        main.startup()
        with main.db() as connection:
            connection.execute("INSERT INTO conversations VALUES(?,?,?,?)", ("c", "Test", main.now(), main.now()))
            connection.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status,model,metadata) VALUES(?,?,?,?,?,?,?)", ("r", "c", "m", main.now(), "started", "Cyber-Tiel", json.dumps({"provider": "local"})))

    def tearDown(self):
        self.main.DB_PATH = self.old_db
        self.directory.cleanup()

    def test_sink_persists_ordered_safe_events_and_finalizes_run(self):
        sink = self.main.SQLiteRuntimeEventSink("r")
        reason = sink.start_event("REASON", "Cyber-Tiel", {"model": "Cyber-Tiel", "round": 1, "prompt": "user secret", "completion": "hidden"})
        action = sink.start_event("ACT", "web_search", {"tool": "web_search", "arguments": {"q": "private"}}, reason)
        sink.finish_event(action, "failed", {"tool": "web_search", "error_code": "tool_execution_error", "raw_result": "private"}, 7.5)
        sink.finish_event(reason, "completed", {"model": "Cyber-Tiel", "round": 1}, 12.0)
        self.main._finish_runtime_run("r", "completed")

        with self.main.db() as connection:
            run = connection.execute("SELECT * FROM runtime_runs WHERE id='r'").fetchone()
            events = connection.execute("SELECT * FROM runtime_events WHERE run_id='r' ORDER BY sequence").fetchall()
        self.assertEqual(run["status"], "completed")
        self.assertEqual([event["kind"] for event in events], ["REASON", "ACT"])
        self.assertEqual(events[1]["parent_event_id"], reason)
        serialized = json.dumps([dict(run), *(dict(event) for event in events)])
        for secret in ("api_key", "Authorization", "prompt", "completion", "private", "arguments", "raw_result"):
            self.assertNotIn(secret, serialized)

    def test_run_api_returns_safe_details_and_filters(self):
        sink = self.main.SQLiteRuntimeEventSink("r")
        sink.finish_event(sink.start_event("REASON", "Cyber-Tiel", {"model": "Cyber-Tiel", "round": 1}), "completed", {"model": "Cyber-Tiel"}, 3)
        runs = self.main.runtime_runs(conversation_id="c", message_id="m", run_id="r", limit=10)
        details = self.main.runtime_run("r")
        self.assertEqual(runs[0]["id"], "r")
        self.assertEqual(details["events"][0]["kind"], "REASON")
        self.assertNotIn("content", json.dumps(details))


if __name__ == "__main__":
    unittest.main()
