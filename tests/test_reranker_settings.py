import tempfile
import unittest
from pathlib import Path


class RerankerSettingsTests(unittest.TestCase):
    def test_candidate_limit_is_persisted_by_settings_endpoint(self):
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            previous = main.DB_PATH
            main.DB_PATH = Path(directory) / "settings.sqlite3"
            try:
                main.startup()
                with main.db() as connection:
                    connection.execute("INSERT INTO providers VALUES('p','P','https://p.test/v1','','now')")
                    connection.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES('embed','p','embed','[\"embedding\"]')")
                    connection.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES('rank','p','rank','[\"reranking\"]')")
                config = main.EmbeddingConfigurationIn(provider_id="p", model_id="embed", reranking_enabled=True,
                    reranker_provider_id="p", reranker_model="rank", reranker_candidate_limit=8)
                result = main.save_embedding_settings(config)
                self.assertEqual(result["configuration"]["reranker_candidate_limit"], 8)
                self.assertEqual(main.embedding_configuration().reranker_candidate_limit, 8)
            finally:
                main.DB_PATH = previous


if __name__ == "__main__":
    unittest.main()
