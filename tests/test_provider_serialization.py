import asyncio
import json
import unittest

from fastapi import FastAPI

from app.agent import AgentRuntime
from app.kernel import ModuleRegistry, ToolDefinition, ToolExecutionContext


class Response:
    status_code = 200

    async def aiter_lines(self):
        yield 'data: ' + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
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
            return [event async for event in AgentRuntime(registry).stream(
                client, "https://omlx.example/v1/chat/completions", {},
                [{"role": "user", "content": "current news"}], "Cyber-Tiel",
                {"tool-calling"}, ToolExecutionContext("conversation", "provider", "Cyber-Tiel", 0),
            )]

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


if __name__ == "__main__":
    unittest.main()
