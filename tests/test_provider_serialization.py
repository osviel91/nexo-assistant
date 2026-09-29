import asyncio
import json
import unittest

from fastapi import FastAPI

from app.agent_model import OpenAICompatibleModelAdapter
from app.kernel import ModuleRegistry, ToolDefinition, ToolExecutionContext


class Response:
    status_code = 200

    async def aiter_lines(self):
        yield 'data: ' + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        yield 'data: ' + json.dumps({"choices": [{}], "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}})
        yield "data: [DONE]"


class Stream:
    async def __aenter__(self):
        return Response()

    async def __aexit__(self, *args):
        return None


class Client:
    def __init__(self):
        self.calls = []

    def stream(self, method, url, headers, json):
        self.calls.append((method, url, json))
        return Stream()


class ProviderSerializationTests(unittest.TestCase):
    def test_chat_completions_request_contains_web_search_schema(self):
        registry = ModuleRegistry(FastAPI())

        async def search(_context, _arguments):
            return {"results": []}

        registry.context.tools.register(ToolDefinition(
            "web_search", "Search", {"type": "object", "required": ["query"]}, search,
        ))
        client = Client()

        async def run():
            adapter = OpenAICompatibleModelAdapter(client, "https://omlx.example/v1/chat/completions", {}, "Cyber-Tiel")
            tools = registry.tool_definitions(ToolExecutionContext("conversation", "provider", "Cyber-Tiel", 0), {"tool-calling"})
            return [event async for event in adapter.stream([{"role": "user", "content": "current news"}], tools)]

        asyncio.run(run())
        method, endpoint, payload = client.calls[0]
        self.assertEqual((method, endpoint), ("POST", "https://omlx.example/v1/chat/completions"))
        self.assertEqual(payload["tools"][0], {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search",
                "parameters": {"type": "object", "required": ["query"]},
            },
        })

    def test_adapter_preserves_physical_model_and_usage(self):
        client = Client()

        async def run():
            adapter = OpenAICompatibleModelAdapter(client, "https://provider.example/v1/chat/completions", {}, "Gemma4-e2b")
            return [event async for event in adapter.stream([{"role": "system", "content": "Be brief"}, {"role": "user", "content": "hi"}], [])]

        events = asyncio.run(run())
        self.assertEqual(client.calls[0][2]["model"], "Gemma4-e2b")
        self.assertEqual(client.calls[0][2]["messages"][0]["role"], "system")
        self.assertEqual(events[-1].usage["total_tokens"], 6)
        self.assertIsNotNone(events[0].provider_ttft_ms)


if __name__ == "__main__":
    unittest.main()
