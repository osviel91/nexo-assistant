import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.knowledge import bootstrap_values, validate_configuration
from app.migrations import migrate


class Stage11BDefaultTests(unittest.TestCase):
    def test_default_is_eight_and_explicit_twenty_is_unchanged(self):
        values = {"provider_id": "p", "model_id": "m", "target_chunk_size": 10,
                  "max_chunk_size": 20, "overlap": 1, "batch_size": 1,
                  "retrieval_top_k": 5, "retrieval_max_context_chars": 1000}
        self.assertEqual(validate_configuration(values)["reranker_candidate_limit"], 8)
        self.assertEqual(validate_configuration({**values, "reranker_candidate_limit": 20})["reranker_candidate_limit"], 20)
        self.assertEqual(bootstrap_values()["reranker_candidate_limit"], "8")

    def test_persisted_twenty_is_read_without_rewrite(self):
        import app.main as main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.sqlite3"
            with sqlite3.connect(path) as connection:
                migrate(connection)
                connection.execute("INSERT INTO providers VALUES ('p','Provider','http://provider','','now')")
                connection.execute("""INSERT INTO embedding_configurations
                    (id,provider_id,model_id,target_chunk_size,max_chunk_size,overlap,batch_size,
                     retrieval_top_k,retrieval_max_context_chars,config_version,created_at,updated_at,
                     reranker_candidate_limit)
                    VALUES ('c','p','m',10,20,1,1,5,1000,1,'now','now',20)""")
            previous_path = main.DB_PATH
            main.DB_PATH = path
            try:
                self.assertEqual(main.embedding_configuration().reranker_candidate_limit, 20)
                with sqlite3.connect(path) as connection:
                    self.assertEqual(connection.execute("SELECT reranker_candidate_limit FROM embedding_configurations WHERE id='c'").fetchone()[0], 20)
            finally:
                main.DB_PATH = previous_path


if __name__ == "__main__":
    unittest.main()
