import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi import FastAPI

from app.artifacts import ArtifactError, sandbox_document, validate_artifact
from app.kernel import ModuleContext, ToolExecutionContext
from app.native_tools import register_native_tools


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.context = ModuleContext(FastAPI())
        register_native_tools(self.context, lambda: datetime(2026, 9, 29, 21, 42, 18, tzinfo=timezone.utc))
        self.execution = ToolExecutionContext("c", "p", "m", 1)

    def test_native_tools_register_and_datetime_timezone(self):
        tools = self.context.tools.registered_tools()
        self.assertEqual({tool.name for tool in tools}, {"native.get_current_datetime", "native.render_artifact"})
        result = asyncio.run(self.context.tools.invoke("native.get_current_datetime", self.execution, {"timezone": "Europe/Madrid"}))
        self.assertEqual(result["iso"], "2026-09-29T23:42:18+02:00")
        self.assertEqual(result["weekday"], "Tuesday")
        invalid = asyncio.run(self.context.tools.invoke("native.get_current_datetime", self.execution, {"timezone": "Mars/Olympus"}))
        self.assertEqual(invalid["error"]["code"], "invalid_timezone")

    def test_valid_artifact_types_and_table_limits(self):
        for kind, data in [
            ("table", {"columns": ["n"], "rows": [[1]]}),
            ("bar", {"labels": ["a"], "series": [{"values": [1]}]}),
            ("line", {"labels": ["a"], "series": [{"values": [1]}]}),
            ("pie", {"labels": ["a"], "series": [{"values": [1]}]}),
            ("scatter", {"points": [{"x": 1, "y": 2}]}),
            ("metrics", [{"label": "p50", "value": 3}]),
            ("html", {"html": "<h1>Hi</h1><script>alert(1)</script>"}),
        ]:
            result = validate_artifact({"type": kind, "title": "Example", "data": data})
            self.assertEqual(result["schema_version"], 1)
            self.assertTrue(result["id"])
        with self.assertRaises(ArtifactError):
            validate_artifact({"type": "table", "title": "Too big", "data": {"columns": ["x"], "rows": [[0]] * 501}})
        with self.assertRaises(ArtifactError):
            validate_artifact({"type": "bar", "title": "Mismatch", "data": {"labels": ["a"], "series": [{"values": [1, 2]}]}})

    def test_render_tool_errors_and_html_sandbox_contract(self):
        invalid = asyncio.run(self.context.tools.invoke("native.render_artifact", self.execution, {"type": "bar"}))
        self.assertEqual(invalid["error"]["code"], "invalid_artifact")
        artifact = asyncio.run(self.context.tools.invoke("native.render_artifact", self.execution, {"type": "html", "title": "T", "data": {"html": "<script>x</script><p>safe</p>"}}))["artifacts"][0]
        self.assertNotIn("<script", artifact["data"]["html"])
        doc = sandbox_document(artifact["data"]["html"])
        self.assertIn("default-src 'none'", doc)

    def test_render_tool_schema_and_non_table_payloads_match_validator(self):
        tool = next(tool for tool in self.context.tools.registered_tools() if tool.name == "native.render_artifact")
        data_shapes = tool.parameters["properties"]["data"]["anyOf"]
        self.assertIn("labels", next(shape["properties"] for shape in data_shapes if "series" in shape.get("properties", {})))
        self.assertIn("No envíes data como texto JSON", tool.description)
        for kind, data in (
            ("metrics", [{"label": "p50", "value": 12}]),
            ("bar", {"labels": ["Jan", "Feb"], "series": [{"name": "Sales", "values": [12, 15]}]}),
        ):
            result = asyncio.run(self.context.tools.invoke("native.render_artifact", self.execution, {"type": kind, "title": "Example", "data": data}))
            self.assertEqual(result["artifacts"][0]["type"], kind)

    def test_chart_tool_reports_stringified_dataset_and_accepts_object_dataset(self):
        invalid = asyncio.run(self.context.tools.invoke("native.render_artifact", self.execution, {"type": "bar", "title": "Sales", "data": '{"labels":["Jan"],"series":[{"values":[12]}]}'}))
        self.assertIn("no envíes data como texto JSON", invalid["error"]["message"])
        valid = asyncio.run(self.context.tools.invoke("native.render_artifact", self.execution, {"type": "bar", "title": "Sales", "data": {"labels": ["Jan"], "series": [{"values": [12]}]}}))
        self.assertEqual(valid["artifacts"][0]["data"]["series"][0]["values"], [12])

    def test_artifact_survives_reload_and_branch(self):
        from pathlib import Path
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            previous = main.DB_PATH
            main.DB_PATH = Path(directory) / "artifacts.sqlite3"
            main.startup()
            artifact = validate_artifact({"type": "metrics", "title": "Latency", "data": [{"label": "p50", "value": 12}]})
            with main.db() as connection:
                connection.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES('c','Chat','now','now')")
                connection.execute("INSERT INTO messages(id,conversation_id,role,content,created_at,artifacts) VALUES('m','c','assistant','answer','now',?)", (json.dumps([artifact]),))
            restored = main.get_conversation("c")["messages"][0]["artifacts"]
            branch_id = main.branch_conversation("c", "m")["id"]
            branched = main.get_conversation(branch_id)["messages"][0]["artifacts"]
            main.DB_PATH = previous
        self.assertEqual(restored, [artifact])
        self.assertEqual(branched, [artifact])

if __name__ == "__main__":
    unittest.main()
