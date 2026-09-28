import asyncio
import json
import unittest

from app.agent import AgentRuntime, AgentRuntimeLimits
from app.kernel import ModuleRegistry, ToolDefinition, ToolExecutionContext
from fastapi import FastAPI


class Response:
    status_code = 200

    def __init__(self, chunks):
        self.chunks = chunks

    async def aiter_lines(self):
        for chunk in self.chunks:
            yield "data: " + json.dumps({"choices": [{"delta": chunk}]})
        yield "data: [DONE]"


class Stream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return None


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.payloads = []

    def stream(self, method, url, headers, json):
        self.payloads.append(json)
        return Stream(Response(next(self.responses)))


def run(runtime, client, capabilities={"tool-calling"}):
    async def collect():
        return [event async for event in runtime.stream(
            client, "http://provider/chat", {}, [{"role": "user", "content": "x"}], "model",
            capabilities, ToolExecutionContext("conversation", "provider", "model", 0),
        )]
    return asyncio.run(collect())


class AgentRuntimeTests(unittest.TestCase):
    def registry(self, handler):
        registry = ModuleRegistry(FastAPI())
        registry.context.tools.register(ToolDefinition("test_tool", "test", {"type": "object"}, handler))
        return registry

    def test_context_and_successful_execution(self):
        seen = []

        async def handler(context, arguments):
            seen.append((context, arguments))
            return {"results": [{"title": "Source", "url": "https://source.test"}]}

        client = Client([
            [{"tool_calls": [{"index": 0, "id": "call", "function": {"name": "test_tool", "arguments": '{"q":"x"}'}}]}],
            [{"content": "answer [1]"}],
        ])
        events = run(AgentRuntime(self.registry(handler)), client)
        self.assertEqual(seen[0][0], ToolExecutionContext("conversation", "provider", "model", 1))
        self.assertEqual(seen[0][1], {"q": "x"})
        self.assertEqual(events[-1]["sources"], [{"title": "Source", "url": "https://source.test"}])
        self.assertEqual(events[-1]["tools_used"], ["test_tool"])
        self.assertEqual(events[-1]["tool_rounds"], 1)
        trace_events = [event["trace"] for event in events if "trace" in event]
        self.assertEqual([event["type"] for event in trace_events], ["REASON", "ACT", "REASON"])
        self.assertNotIn("arguments", json.dumps(trace_events))
        self.assertIn("tools", client.payloads[0])

    def test_invalid_unknown_and_handler_errors_are_safe(self):
        async def broken(context, arguments):
            raise RuntimeError("secret")

        for name, arguments, code in (("missing", "{}", "unknown_tool"), ("test_tool", "not-json", "invalid_arguments"), ("test_tool", "{}", "tool_execution_error")):
            registry = self.registry(broken)
            client = Client([
                [{"tool_calls": [{"index": 0, "id": "call", "function": {"name": name, "arguments": arguments}}]}],
                [{"content": "continued"}],
            ])
            events = run(AgentRuntime(registry), client)
            self.assertTrue(any(event.get("status") == "tool_error" for event in events))
            self.assertNotIn("secret", json.dumps(client.payloads[1]))
            self.assertIn(code, client.payloads[1]["messages"][-1]["content"])

    def test_models_without_tool_capability_do_not_receive_tools(self):
        async def handler(context, arguments):
            return {}

        client = Client([[{"content": "plain"}]])
        run(AgentRuntime(self.registry(handler)), client, set())
        self.assertNotIn("tools", client.payloads[0])

    def test_shadow_branch_cannot_change_agent_payload(self):
        async def handler(context, arguments):
            return {}

        first = Client([[{"content": "plain"}]])
        second = Client([[{"content": "plain"}]])
        run(AgentRuntime(self.registry(handler)), first, set())
        run(AgentRuntime(self.registry(handler)), second, set())
        self.assertEqual(first.payloads, second.payloads)

    def test_round_limit_and_output_limit(self):
        async def handler(context, arguments):
            return {"value": "x" * 1000}

        responses = [[{"tool_calls": [{"index": 0, "id": str(i), "function": {"name": "test_tool", "arguments": "{}"}}]}] for i in range(4)]
        client = Client(responses)
        events = run(AgentRuntime(self.registry(handler), AgentRuntimeLimits(3, 100)), client)
        self.assertEqual(len(client.payloads), 4)
        self.assertEqual(events[-1]["error"], "Se alcanzó el límite de rondas de herramientas.")
        self.assertLessEqual(len(client.payloads[1]["messages"][-1]["content"]), 100)


if __name__ == "__main__":
    unittest.main()
