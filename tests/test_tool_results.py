import unittest
from datetime import date, timedelta

from app.tool_results import ToolResultPipeline, aemet_projector
from app.artifacts import validate_artifact


class ToolResultPipelineTests(unittest.TestCase):
    def test_large_structured_result_is_valid_and_keeps_representative_records(self):
        raw = {"structured_data": {"columns": ["id", "value"], "rows": [[i, "x" * 30] for i in range(500)]}}
        canonical, metadata = ToolResultPipeline(1400).process(raw)
        projection = metadata["projection"]
        self.assertEqual(len(canonical["structured_data"]["rows"]), 500)
        self.assertTrue(metadata["truncated"])
        self.assertLessEqual(metadata["projected_size"], 1400)
        self.assertEqual(projection["structured_data"]["columns"], ["id", "value"])
        self.assertGreater(len(projection["structured_data"]["rows"]), 0)
        self.assertTrue(projection["result_metadata"]["truncated"])
        self.assertEqual(projection["result_metadata"]["total_records"], 500)
        artifact = validate_artifact({"type": "table", "title": "Compacted", "data": projection["structured_data"]})
        self.assertEqual(artifact["type"], "table")

    def test_small_result_is_unchanged(self):
        result = {"content": "ok", "structured_data": {"n": 2}}
        canonical, metadata = ToolResultPipeline().process(result)
        self.assertEqual(canonical, result)
        self.assertFalse(metadata["compacted"])

    def test_text_truncation_is_marked(self):
        _, metadata = ToolResultPipeline(400).process({"content": "x" * 2000})
        self.assertTrue(metadata["truncated"])
        self.assertTrue(metadata["projection"]["result_metadata"]["truncated"])

    def test_generic_mcp_structured_projection_keeps_schema_and_identifiers(self):
        rows = [{"id": f"item-{i}", "title": f"Vault record {i}", "payload": "x" * 300} for i in range(80)]
        canonical, metadata = ToolResultPipeline(1600).process({"structured_data": {"schema": ["id", "title"], "results": rows}})
        self.assertEqual(len(canonical["structured_data"]["results"]), 80)
        projection = metadata["projection"]["structured_data"]
        self.assertEqual(projection["schema"], ["id", "title"])
        self.assertTrue(projection["results"][0]["id"].startswith("item-"))
        self.assertLessEqual(metadata["projected_size"], 1600)

    def test_aemet_projection_prioritizes_requested_city_and_tomorrow(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        raw = {"data": {"predictions": [
            {"fecha": "2099-01-01", "municipio": "Barcelona", "temperatura": 20},
            {"fecha": tomorrow, "municipio": "Madrid", "temperatura": 24},
        ]}, "metadata": {"provider": "AEMET"}}
        _, metadata = ToolResultPipeline(1000).process(raw, query="temperatura Madrid mañana", projector=aemet_projector)
        records = metadata["projection"]["data"]["predictions"]
        self.assertEqual(records[0]["municipio"], "Madrid")
        self.assertEqual(records[0]["fecha"], tomorrow)

    def test_bad_projector_and_malformed_data_fall_back_safely(self):
        canonical, metadata = ToolResultPipeline(500).process({"data": [object()]}, projector=lambda *_: None)
        self.assertTrue(metadata["truncated"] or metadata["projection_strategy"] == "structured_generic")
        self.assertIsInstance(canonical, dict)
        _, small = ToolResultPipeline(100).process({"content": "x" * 5000}, projector=lambda *_: (_ for _ in ()).throw(RuntimeError()))
        self.assertLessEqual(small["projected_size"], 100)
        cyclic = {}
        cyclic["self"] = cyclic
        canonical, _ = ToolResultPipeline().process(cyclic)
        self.assertIn("could not be serialized", canonical["content"])


if __name__ == "__main__":
    unittest.main()
