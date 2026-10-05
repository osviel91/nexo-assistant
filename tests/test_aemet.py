import asyncio
from datetime import date, timedelta
import json
import unittest

import httpx
from fastapi import FastAPI

from app.kernel import ModuleContext, ToolExecutionContext
from app.modules.aemet import compact_data, filter_records, register_aemet_tool
from app.modules.aemet import MAX_RESPONSE_BYTES
from app.tool_results import ToolResultPipeline


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

    def test_large_forecast_reaches_aemet_projection_and_provider_continuation(self):
        from app.kernel import ModuleRegistry, ToolDefinition
        from app.tools import ExposurePolicy, ToolExecutor
        from app.agent import AgentRunRequest, AgentRuntime
        from app.agent_model import ModelStreamChunk
        from app.tool_results import aemet_projector

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        forecast = [{"municipio": "Madrid", "id": "28079", "fecha": tomorrow,
                     "prediccion": {"dia": [{"fecha": tomorrow, "temperatura": {"maxima": 24}}]},
                     "observaciones": "Pronóstico municipal " + ("cielo despejado; " * 80)}]
        forecast.extend({"municipio": f"Localidad {i}", "id": str(i), "fecha": tomorrow,
                         "observaciones": "Pronóstico " + ("sin datos relevantes; " * 80)} for i in range(180))
        body = json.dumps(forecast, ensure_ascii=False).encode()
        self.assertGreater(len(body), 12_000)
        self.assertLess(len(body), MAX_RESPONSE_BYTES)

        def handler(request):
            if request.url.path.endswith("/data"):
                return httpx.Response(200, content=body)
            return httpx.Response(200, json={"estado": 200, "datos": "https://opendata.aemet.es/data"})

        aemet_tool = self.make_tool(handler)
        pipeline_input = self.call(aemet_tool, {"path": "/api/prediccion/especifica/municipio/diaria/28079"})
        self.assertEqual(pipeline_input["data"][0]["id"], "28079")

        class Adapter:
            model_id = "test"
            payloads = []
            async def stream(self, messages, tools, temperature=None):
                self.payloads.append(messages)
                if len(self.payloads) == 1:
                    yield ModelStreamChunk(tool_calls=[{"index": 0, "id": "aemet-call", "function": {
                        "name": "aemet.opendata", "arguments": '{"path":"/api/prediccion/especifica/municipio/diaria/28079"}'}}])
                else:
                    yield ModelStreamChunk(content="Pronóstico recibido.")

        async def handler_tool(_context, _arguments):
            return pipeline_input

        registry = ModuleRegistry(FastAPI())
        registry.context.tools.register(ToolDefinition("aemet.opendata", "AEMET", {"type": "object"}, handler_tool,
            source="aemet", action="read_only", result_projector=aemet_projector))
        adapter = Adapter()
        async def run_agent():
            tools = ExposurePolicy().resolve(registry.tool_catalog_view(), {"tool-calling"})
            request = AgentRunRequest(adapter, [{"role": "user", "content": "¿Qué tiempo hará mañana en Madrid?"}], tools,
                ToolExecutor(), ToolExecutionContext("c", "p", "m", 0))
            return [event async for event in AgentRuntime().stream(request)]
        events = asyncio.run(run_agent())
        continuation = next(message["content"] for message in adapter.payloads[1] if message.get("role") == "tool")
        self.assertIn('"id":"28079"', continuation)
        self.assertIn(tomorrow, continuation)
        self.assertEqual(events[-1]["answer"], "Pronóstico recibido.")

    def test_data_http_error_keeps_safe_status_and_category(self):
        def handler(request):
            return httpx.Response(200, json={"datos": "https://opendata.aemet.es/data"}) if not request.url.path.endswith("/data") else httpx.Response(503)
        error = self.call(self.make_tool(handler), {"path": "/api/test"})["error"]
        self.assertEqual((error["code"], error["upstream_status"], error["upstream_category"]), ("aemet_data_error", 503, "http_error"))

    def test_filters_master_records_locally_by_municipality_name(self):
        records = [{"id": "28161", "nombre": "Valdemoro"}, {"id": "28079", "nombre": "Madrid"}]
        self.assertEqual(filter_records(records, "valdemoro"), [records[0]])

    def test_semantic_forecast_resolves_madrid_and_uses_one_model_call(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/maestro/municipios"):
                return httpx.Response(200, json={"datos": "https://opendata.aemet.es/catalogue"})
            if request.url.path.endswith("/catalogue"):
                return httpx.Response(200, json=[{"id": "28079", "nombre": "Madrid"}])
            if request.url.path.endswith("/municipio/diaria/28079"):
                return httpx.Response(200, json={"datos": "https://opendata.aemet.es/forecast"})
            return httpx.Response(200, json=[{"fecha": tomorrow, "temperatura": {"maxima": 24}}])
        tool = self.make_tool(handler)
        self.assertEqual(tool.parameters["properties"]["operation"]["enum"], ["forecast_daily", "raw"])
        result = self.call(tool, {"operation": "forecast_daily", "location": "Madrid", "period": "tomorrow"})
        self.assertEqual(result["structured_data"]["location"]["municipality_code"], "28079")
        self.assertEqual(result["structured_data"]["forecast"][0]["fecha"], tomorrow)
        self.assertEqual(len(requests), 4)  # Catalogue metadata+data, forecast metadata+data.

    def test_explicit_code_skips_catalogue_and_unknown_location_is_bounded(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        seen = []
        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"datos": "https://opendata.aemet.es/data"}) if not request.url.path.endswith("/data") else httpx.Response(200, json=[{"fecha": tomorrow}])
        tool = self.make_tool(handler)
        result = self.call(tool, {"operation": "forecast_daily", "municipality_code": "28079", "date": tomorrow})
        self.assertEqual(len(seen), 2)
        self.assertEqual(result["structured_data"]["location"]["municipality_code"], "28079")

        unknown = self.make_tool(lambda request: httpx.Response(200, json={"datos": "https://opendata.aemet.es/catalogue"}) if request.url.path.endswith("/maestro/municipios") else httpx.Response(200, json=[]))
        error = self.call(unknown, {"operation": "forecast_daily", "location": "No existe", "period": "tomorrow"})["error"]
        self.assertEqual(error["code"], "location_not_found")
        self.assertEqual(error["candidates"], [])

    def test_semantic_failures_are_actionable_and_ambiguity_does_not_guess(self):
        for status in (404, 500, 503):
            tool = self.make_tool(lambda request, status=status: httpx.Response(status))
            error = self.call(tool, {"operation": "forecast_daily", "municipality_code": "28079", "period": "tomorrow"})["error"]
            self.assertEqual((error["code"], error["upstream_status"]), ("upstream_http_error", status))

        def ambiguous(request):
            if request.url.path.endswith("/maestro/municipios"):
                return httpx.Response(200, json={"datos": "https://opendata.aemet.es/catalogue"})
            return httpx.Response(200, json=[{"id": "01001", "nombre": "San José"}, {"id": "02002", "nombre": "San Jose"}])
        error = self.call(self.make_tool(ambiguous), {"operation": "forecast_daily", "location": "SAN JOSE", "period": "tomorrow"})["error"]
        self.assertEqual(error["code"], "location_ambiguous")
        self.assertEqual([item["municipality_code"] for item in error["candidates"]], ["01001", "02002"])

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
