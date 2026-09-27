import asyncio
import json
import unittest

import httpx
from fastapi import FastAPI

from app.kernel import ModuleContext, ToolExecutionContext, ToolRegistry
from app.modules.web_search_searxng import WebSearchSearxngModule


def make_tool(handler, **settings):
    registry = ToolRegistry()
    context = ModuleContext(FastAPI(), {"searxng_url": "http://searxng", **settings}, registry)
    WebSearchSearxngModule(lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)).register(context)
    return registry._tools["web_search"]


class WebSearchTests(unittest.TestCase):
    def run_tool(self, tool, arguments):
        return asyncio.run(tool.handler(ToolExecutionContext("c", "p", "m", 1), arguments))

    def test_active_registers_and_disabled_registry_has_no_tool(self):
        registry = ToolRegistry()
        WebSearchSearxngModule(lambda **kwargs: httpx.AsyncClient()).register(ModuleContext(FastAPI(), {"searxng_url": "http://searxng"}, registry))
        self.assertEqual([item["function"]["name"] for item in registry.definitions()], ["web_search"])
        self.assertEqual(ToolRegistry().definitions(), [])

    def test_query_parameters_and_result_sanitization(self):
        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"results": [
                {"title": "One", "url": "https://one.test", "content": "snippet"},
                {"title": "Bad", "url": "javascript:alert(1)", "content": "drop"},
                {"title": "Two", "url": "http://two.test", "content": "second"},
            ]})

        tool = make_tool(handler, searxng_language="es", searxng_safesearch=2, searxng_max_results=1)
        result = self.run_tool(tool, {"query": "  gatos  "})
        self.assertEqual(seen, {"q": "gatos", "format": "json", "language": "es", "safesearch": "2"})
        self.assertEqual(result["results"], [{"title": "One", "url": "https://one.test", "snippet": "snippet"}])

    def test_invalid_query_and_failure_types_are_explicit(self):
        tool = make_tool(lambda request: httpx.Response(200, json={"results": []}))
        self.assertEqual(self.run_tool(tool, {"query": ""})["error"]["code"], "invalid_query")

        timeout = make_tool(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
        self.assertEqual(self.run_tool(timeout, {"query": "x"})["error"]["code"], "timeout")

        http_error = make_tool(lambda request: httpx.Response(503))
        self.assertEqual(self.run_tool(http_error, {"query": "x"})["error"]["code"], "http_error")

        invalid_json = make_tool(lambda request: httpx.Response(200, content=b"not json"))
        self.assertEqual(self.run_tool(invalid_json, {"query": "x"})["error"]["code"], "invalid_json")

        malformed = make_tool(lambda request: httpx.Response(200, json={"results": [{"title": "missing url"}]}))
        self.assertEqual(self.run_tool(malformed, {"query": "x"}), {"results": []})

    def test_tool_registry_invocation_is_async_and_unknown_is_rejected(self):
        tool = make_tool(lambda request: httpx.Response(200, json={"results": []}))
        registry = ToolRegistry()
        registry.register(tool)
        self.assertEqual(asyncio.run(registry.invoke("web_search", ToolExecutionContext("c", "p", "m", 1), {"query": "x"})), {"results": []})
        with self.assertRaises(KeyError):
            asyncio.run(registry.invoke("other", ToolExecutionContext("c", "p", "m", 1), {}))


if __name__ == "__main__":
    unittest.main()
