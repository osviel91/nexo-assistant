import asyncio
import json
import tempfile
import unittest
from pathlib import Path


class ShadowObservationTests(unittest.TestCase):
    def test_trace_events_are_ordered_and_safe(self):
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            old_db = main.DB_PATH
            main.DB_PATH = Path(directory) / "trace.sqlite3"
            try:
                main.startup()
                first = main._insert_trace_event("conversation-1", "message-1", "DECIDE", "running", {"model": "laya"})
                main._insert_trace_event("conversation-1", "message-1", "ACT", "success", {"tool": "web_search"}, 12)
                main._update_trace_event(first, "success", {"model": "laya", "answers": {"needs_web": {"value": True, "confidence": .8}}}, 8)
                with main.db() as connection:
                    rows = connection.execute("SELECT * FROM runtime_trace_events ORDER BY sequence").fetchall()
                self.assertEqual([row["type"] for row in rows], ["DECIDE", "ACT"])
                self.assertNotIn("Authorization", json.dumps([dict(row) for row in rows]))
            finally:
                main.DB_PATH = old_db

    def test_observation_links_message_and_keeps_execution_facts_without_secrets(self):
        from app import main

        class FakeShadow:
            shadow_enabled = True

            async def shadow_decide(self, message, tools):
                from app.decision.models import ShadowDecision

                self.seen = (message, tools)
                return ShadowDecision("shadow", "laya-shadow", {"needs_web": {"value": True, "confidence": 0.9}}, {"latency_ms": 12}, 13)

        with tempfile.TemporaryDirectory() as directory:
            old_db = main.DB_PATH
            old_service = main.module_registry.context.services.get("decision-shadow")
            main.DB_PATH = Path(directory) / "shadow.sqlite3"
            service = FakeShadow()
            main.module_registry.context.services["decision-shadow"] = service
            try:
                main.startup()
                async def run():
                    execution_future = asyncio.get_running_loop().create_future()
                    execution_future.set_result({"tools_used": ["web_search"], "tool_rounds": 1, "web_search_used": True})
                    await main._run_shadow_observation("message-1", "conversation-1", "latest news", [{"name": "web_search"}], execution_future)
                asyncio.run(run())
                with main.db() as connection:
                    row = connection.execute("SELECT * FROM shadow_observations").fetchone()
                self.assertEqual(row["message_id"], "message-1")
                self.assertEqual(json.loads(row["execution"])["tools_used"], ["web_search"])
                self.assertNotIn("api_key", json.dumps(dict(row)))
                self.assertNotIn("Authorization", json.dumps(dict(row)))
            finally:
                main.DB_PATH = old_db
                if old_service is None:
                    main.module_registry.context.services.pop("decision-shadow", None)
                else:
                    main.module_registry.context.services["decision-shadow"] = old_service


if __name__ == "__main__":
    unittest.main()
