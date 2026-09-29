import unittest
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.capabilities import normalize_model_capabilities, preserve_model_capabilities
from app.migrations import migrate


class CapabilityTests(unittest.TestCase):
    def test_normalizes_supported_tool_shapes(self):
        self.assertIn("tool-calling", normalize_model_capabilities({"capabilities": ["tool-calling"]}))
        self.assertIn("tool-calling", normalize_model_capabilities({"supports_tools": True}))
        self.assertIn("tool-calling", normalize_model_capabilities({"tool_calling": True}))
        self.assertIn("tool-calling", normalize_model_capabilities({"supported_parameters": ["tools"]}))

    def test_unknown_capabilities_require_explicit_fallback(self):
        self.assertNotIn("tool-calling", normalize_model_capabilities({}))
        self.assertIn("tool-calling", normalize_model_capabilities({}, assume_tool_calling=True))

    def test_provider_refresh_preserves_explicit_capabilities(self):
        self.assertEqual(
            preserve_model_capabilities({"tool-calling"}, ["embedding", "reranking"]),
            {"embedding", "reranking", "tool-calling"},
        )

    def test_reranking_is_explicit_and_not_inferred_from_model_name(self):
        self.assertNotIn("reranking", normalize_model_capabilities({"id": "Cyber-Tiel"}))
        self.assertIn("reranking", normalize_model_capabilities({"id": "Cyber-Tiel", "capabilities": ["reranking"]}))

    def test_capability_patch_persists_and_knowledge_uses_provider_scoped_models(self):
        import app.main as main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nexo.sqlite3"
            old_path = main.DB_PATH
            main.DB_PATH = path
            try:
                with main.db() as connection:
                    migrate(connection)
                    connection.execute("INSERT INTO providers VALUES ('a', 'oMLX', 'http://a', '', 'now')")
                    connection.execute("INSERT INTO providers VALUES ('b', 'Other', 'http://b', '', 'now')")
                    connection.execute("INSERT INTO models VALUES ('embedding', 'a', 'embedding', '[]')")
                    connection.execute("INSERT INTO models VALUES ('embedding', 'b', 'embedding', '[\"embedding\"]')")

                main.update_model_capability("a", "embedding", {"capabilities": ["embedding", "reranking"]})
                settings = main.get_embedding_settings()
                provider = next(item for item in settings["providers"] if item["id"] == "a")
                self.assertEqual(provider["models"][0]["capabilities"], ["embedding", "reranking"])
                with main.db() as connection:
                    persisted = connection.execute("SELECT capabilities FROM models WHERE provider_id='a'").fetchone()[0]
                self.assertEqual(json.loads(persisted), ["embedding", "reranking"])

                main.update_model_capability("a", "embedding", {"capabilities": []})
                settings = main.get_embedding_settings()
                self.assertEqual(next(item for item in settings["providers"] if item["id"] == "a")["models"][0]["capabilities"], [])
                self.assertEqual(next(item for item in settings["providers"] if item["id"] == "b")["models"][0]["capabilities"], ["embedding"])

                main.update_model_capability("a", "embedding", {"capabilities": ["embedding", "reranking"]})

                async def discovered_models(*_args, **_kwargs):
                    return {"data": [{"id": "embedding", "capabilities": []}]}

                with patch.object(main, "provider_request", discovered_models):
                    asyncio.run(main.refresh_models("a"))
                with main.db() as connection:
                    persisted = connection.execute("SELECT capabilities FROM models WHERE provider_id='a'").fetchone()[0]
                self.assertEqual(json.loads(persisted), ["embedding", "reranking"])
            finally:
                main.DB_PATH = old_path


if __name__ == "__main__":
    unittest.main()
