import asyncio
import json
import unittest

from app.agent import AgentRunRequest, AgentRuntime, AgentRuntimeLimits
from app.agent_model import ModelAdapterError, ModelStreamChunk
from app.kernel import ModuleRegistry, ToolDefinition, ToolExecutionContext
from app.tools import ExposurePolicy, ToolExecutor
from fastapi import FastAPI


class Adapter:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.payloads = []
        self.model_id = "model"

    async def stream(self, messages, tools, temperature=None):
        payload = {"model": self.model_id, "messages": messages}
        if tools:
            payload["tools"] = tools
        self.payloads.append(payload)
        for chunk in next(self.responses):
            yield ModelStreamChunk(content=chunk.get("content", ""), tool_calls=chunk.get("tool_calls"), usage=chunk.get("usage"))


def run(runtime, adapter, registry, capabilities={"tool-calling"}):
    async def collect():
        context = ToolExecutionContext("conversation", "provider", "model", 0)
        effective_tools = ExposurePolicy().resolve(registry.tool_catalog_view(), capabilities)
        request = AgentRunRequest(adapter, [{"role": "user", "content": "x"}], effective_tools, ToolExecutor(), context)
        return [event async for event in runtime.stream(request)]
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

        adapter = Adapter([
            [{"tool_calls": [{"index": 0, "id": "call", "function": {"name": "test_tool", "arguments": '{"q":"x"}'}}]}],
            [{"content": "answer [1]"}],
        ])
        registry = self.registry(handler)
        events = run(AgentRuntime(), adapter, registry)
        self.assertEqual(seen[0][0], ToolExecutionContext("conversation", "provider", "model", 1))
        self.assertEqual(seen[0][1], {"q": "x"})
        self.assertEqual(events[-1]["sources"], [{"title": "Source", "url": "https://source.test"}])
        self.assertEqual(events[-1]["tools_used"], ["test_tool"])
        self.assertEqual(events[-1]["tool_rounds"], 1)
        trace_events = [event["trace"] for event in events if "trace" in event]
        self.assertEqual([event["type"] for event in trace_events], ["REASON", "ACT", "REASON"])
        self.assertNotIn("arguments", json.dumps(trace_events))
        self.assertIn("tools", adapter.payloads[0])

    def test_invalid_unknown_and_handler_errors_are_safe(self):
        async def broken(context, arguments):
            raise RuntimeError("secret")

        for name, arguments, code in (("missing", "{}", "tool_not_available"), ("test_tool", "not-json", "invalid_arguments"), ("test_tool", "{}", "tool_execution_error")):
            registry = self.registry(broken)
            adapter = Adapter([
                [{"tool_calls": [{"index": 0, "id": "call", "function": {"name": name, "arguments": arguments}}]}],
                [{"content": "continued"}],
            ])
            events = run(AgentRuntime(), adapter, registry)
            self.assertTrue(any(event.get("status") == "tool_error" for event in events))
            self.assertNotIn("secret", json.dumps(adapter.payloads[1]))
            self.assertIn(code, adapter.payloads[1]["messages"][-1]["content"])

    def test_models_without_tool_capability_do_not_receive_tools(self):
        async def handler(context, arguments):
            return {}

        adapter = Adapter([[{"content": "plain"}]])
        registry = self.registry(handler)
        run(AgentRuntime(), adapter, registry, set())
        self.assertNotIn("tools", adapter.payloads[0])

    def test_provider_failure_is_safe_and_trace_has_no_request_content(self):
        class BrokenAdapter:
            model_id = "model"

            async def stream(self, messages, tools, temperature=None):
                raise ModelAdapterError("Provider connection failed: offline")
                yield

        registry = self.registry(lambda _context, _arguments: {"secret": "result"})
        events = run(AgentRuntime(), BrokenAdapter(), registry, set())
        trace = next(event["trace"] for event in events if "trace" in event)
        self.assertEqual(events[-1]["error"], "Provider connection failed: offline")
        self.assertNotIn("content", json.dumps(trace))
        self.assertNotIn("secret", json.dumps(trace))

    def test_shadow_branch_cannot_change_agent_payload(self):
        async def handler(context, arguments):
            return {}

        first = Adapter([[{"content": "plain"}]])
        second = Adapter([[{"content": "plain"}]])
        first_registry = self.registry(handler)
        second_registry = self.registry(handler)
        run(AgentRuntime(), first, first_registry, set())
        run(AgentRuntime(), second, second_registry, set())
        self.assertEqual(first.payloads, second.payloads)

    def test_round_limit_and_output_limit(self):
        async def handler(context, arguments):
            return {"value": "x" * 1000}

        responses = [[{"tool_calls": [{"index": 0, "id": str(i), "function": {"name": "test_tool", "arguments": "{}"}}]}] for i in range(4)]
        adapter = Adapter(responses)
        registry = self.registry(handler)
        events = run(AgentRuntime(AgentRuntimeLimits(3, 100)), adapter, registry)
        self.assertEqual(len(adapter.payloads), 4)
        self.assertEqual(events[-1]["error"], "Se alcanzó el límite de rondas de herramientas.")
        self.assertLessEqual(len(adapter.payloads[1]["messages"][-1]["content"]), 100)

    def test_system_instruction_is_an_independent_message_and_metrics_are_normalized(self):
        adapter = Adapter([[{"content": "answer"}]])
        adapter.responses = iter([[{"content": "answer", "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}]])
        registry = self.registry(lambda _context, _arguments: {})

        async def collect():
            context = ToolExecutionContext("conversation", "provider", "Gemma4-e2b", 0)
            tools = ExposurePolicy().resolve(registry.tool_catalog_view(), set())
            request = AgentRunRequest(adapter, [{"role": "user", "content": "x"}], tools, ToolExecutor(), context,
                                      system_instructions="Agent A instructions", profile_id="agent-a", profile_name="Agent A",
                                      runtime_snapshot={"resolved_provider": "provider", "resolved_model": "Gemma4-e2b", "system_instructions_applied": True})
            return [event async for event in AgentRuntime().stream(request)]

        events = asyncio.run(collect())
        self.assertEqual(adapter.payloads[0]["messages"][0], {"role": "system", "content": "Agent A instructions"})
        self.assertEqual(events[-1]["telemetry"]["completion_tokens"], 2)
        self.assertEqual(events[-1]["telemetry"]["usage_source"], "provider")


if __name__ == "__main__":
    unittest.main()
