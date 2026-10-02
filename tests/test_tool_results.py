import unittest

from app.tool_results import ToolResultPipeline
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


if __name__ == "__main__":
    unittest.main()
