import asyncio
import unittest

import httpx
from fastapi import FastAPI

from app.kernel import ModuleContext, ToolExecutionContext
from app.modules.aemet import compact_data, filter_records, register_aemet_tool


class AemetTests(unittest.TestCase):
    def make_tool(self, handler, key=lambda: "secret"):
        context = ModuleContext(FastAPI())
        register_aemet_tool(context, key, lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
        return context.tools._tools["aemet.opendata"]

    def call(self, tool, arguments):
        return asyncio.run(tool.handler(ToolExecutionContext("c", "p", "m", 1), arguments))

    def test_fetches_metadata_then_data_and_keeps_key_off_data_url(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/data"):
                return httpx.Response(200, json=[{"temperature": 20}])
            return httpx.Response(200, json={"estado": 200, "datos": "https://opendata.aemet.es/data"})

        result = self.call(self.make_tool(handler), {"path": "/api/observacion/convencional/todas", "params": {"estacion": "1234"}})
        self.assertEqual(result["data"], [{"temperature": 20}])
        self.assertEqual(result["metadata"], {"estado": 200})
        self.assertEqual(requests[0].url.params["api_key"], "secret")
        self.assertNotIn("api_key", requests[1].url.params)

    def test_rejects_arbitrary_paths_and_untrusted_data_urls(self):
        tool = self.make_tool(lambda request: httpx.Response(200, json={"datos": "https://example.com/steal"}))
        self.assertEqual(self.call(tool, {"path": "https://example.com"})["error"]["code"], "invalid_path")
        self.assertEqual(self.call(tool, {"path": "/api/test"})["error"]["code"], "invalid_data_url")

        no_key = self.make_tool(lambda request: self.fail("must not make a request"), lambda: None)
        self.assertEqual(self.call(no_key, {"path": "/api/test"})["error"]["code"], "not_configured")

    def test_compacts_embedded_maps_and_stops_on_rate_limit(self):
        forecast = {"prediccion": {"dia": [{"fecha": "2026-10-01", "temperatura": {"maxima": 20}}]}, "mapa": "A" * 100_000}
        compacted = compact_data(forecast)
        self.assertEqual(compacted["prediccion"], forecast["prediccion"])
        self.assertNotIn("mapa", compacted)

        tool = self.make_tool(lambda request: httpx.Response(429))
        self.assertEqual(self.call(tool, {"path": "/api/test"})["error"]["code"], "rate_limited")

    def test_filters_master_records_locally_by_municipality_name(self):
        records = [{"id": "28161", "nombre": "Valdemoro"}, {"id": "28079", "nombre": "Madrid"}]
        self.assertEqual(filter_records(records, "valdemoro"), [records[0]])

    def test_settings_store_token_without_returning_it_and_allow_clear(self):
        from app import main
        from fastapi.testclient import TestClient
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            old_db = main.DB_PATH
            main.DB_PATH = Path(directory) / "settings.sqlite3"
            main.startup()
            try:
                with TestClient(main.app) as client:
                    saved = client.put("/api/settings/tools", json={"max_tool_calls": 10, "aemet_api_key": "my-secret"})
                    self.assertEqual(saved.json(), {"max_tool_calls": 10, "has_aemet_api_key": True})
                    self.assertNotIn("my-secret", saved.text)
                    self.assertEqual(main.aemet_api_key(), "my-secret")
                    client.put("/api/settings/tools", json={"max_tool_calls": 10, "clear_aemet_api_key": True})
                    self.assertIsNone(main.aemet_api_key())
            finally:
                main.DB_PATH = old_db


if __name__ == "__main__":
    unittest.main()
