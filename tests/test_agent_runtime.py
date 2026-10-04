import asyncio
import json
import unittest
from unittest.mock import patch

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
            yield ModelStreamChunk(content=chunk.get("content", ""), reasoning_content=chunk.get("reasoning_content", ""), tool_calls=chunk.get("tool_calls"), usage=chunk.get("usage"), provider_ttft_ms=chunk.get("provider_ttft_ms"))


def run(runtime, adapter, registry, capabilities={"tool-calling"}, request_started_at=None):
    async def collect():
        context = ToolExecutionContext("conversation", "provider", "model", 0)
        effective_tools = ExposurePolicy().resolve(registry.tool_catalog_view(), capabilities)
        request = AgentRunRequest(adapter, [{"role": "user", "content": "x"}], effective_tools, ToolExecutor(), context,
                                  request_started_at=request_started_at)
        return [event async for event in runtime.stream(request)]
    return asyncio.run(collect())


class AgentRuntimeTests(unittest.TestCase):
    def registry(self, handler):
        registry = ModuleRegistry(FastAPI())
        registry.context.tools.register(ToolDefinition("test_tool", "test", {"type": "object"}, handler, action="read_only"))
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

    def test_progress_guard_failures_fail_open_before_and_after_tool_call(self):
        from app.progress_guard import ProgressGuard

        original_pre = ProgressGuard.observe_call
        original_post = ProgressGuard.observe_result
        for failing_method, original in (("observe_call", original_pre), ("observe_result", original_post)):
            async def handler(_context, _arguments):
                return {"structured_data": {"rows": [[1]]}}

            adapter = Adapter([
                [{"tool_calls": [{"index": 0, "id": "mcp-call", "function": {"name": "test_tool", "arguments": "{}"}}]}],
                [{"content": "continued"}],
            ])
            with patch.object(ProgressGuard, failing_method, side_effect=RuntimeError("guard failure")):
                events = run(AgentRuntime(), adapter, self.registry(handler))
            self.assertEqual(events[-1]["answer"], "continued")
            self.assertEqual(adapter.payloads[1]["messages"][-1]["tool_call_id"], "mcp-call")
            setattr(ProgressGuard, failing_method, original)

    def test_recovery_keeps_tool_result_and_hard_stop_requests_final_turn_without_tools(self):
        calls = []

        async def handler(_context, _arguments):
            calls.append(True)
            return {"content": "vault evidence"}

        tool_call = {"tool_calls": [{"index": 0, "id": "vault-call", "function": {"name": "test_tool", "arguments": "{}"}}]}
        adapter = Adapter([[tool_call], [tool_call], [tool_call], [{"content": "final from evidence"}]])
        events = run(AgentRuntime(), adapter, self.registry(handler))

        self.assertEqual(len(calls), 2)
        self.assertEqual(events[-1]["answer"], "final from evidence")
        final_payload = adapter.payloads[-1]
        self.assertNotIn("tools", final_payload)
        self.assertTrue(any(message.get("role") == "tool" and message["tool_call_id"] == "vault-call"
                            for message in final_payload["messages"]))
        self.assertTrue(any(message.get("role") == "system" and "stopped" in message["content"]
                            for message in final_payload["messages"]))

    def test_tool_enabled_turn_tells_model_to_use_and_interpret_available_tools(self):
        adapter = Adapter([[{"content": "answer"}]])
        events = run(AgentRuntime(), adapter, self.registry(lambda *_: {}))
        system_message = next(message for message in adapter.payloads[0]["messages"] if message["role"] == "system")
        self.assertIn("test_tool", system_message["content"])
        self.assertIn("interpret", system_message["content"].lower())

    def test_reasoning_deltas_are_streamed_separately_from_answer_content(self):
        adapter = Adapter([[{"reasoning_content": "private reasoning", "content": "visible answer"}]])
        events = run(AgentRuntime(), adapter, self.registry(lambda *_: {}), capabilities=set())
        self.assertIn({"thinking_delta": "private reasoning"}, events)
        self.assertEqual(events[-1]["answer"], "visible answer")

    def test_provider_ttft_and_request_to_first_token_use_distinct_starts(self):
        import time

        adapter = Adapter([[{"content": "answer", "provider_ttft_ms": 12}]])
        events = run(AgentRuntime(), adapter, self.registry(lambda *_: None), capabilities=set(),
                     request_started_at=time.perf_counter() - 0.05)
        telemetry = events[-1]["telemetry"]
        self.assertEqual(telemetry["provider_ttft_ms"], 12)
        self.assertGreaterEqual(telemetry["request_to_first_token_ms"], 40)
        self.assertNotEqual(telemetry["provider_ttft_ms"], telemetry["request_to_first_token_ms"])

    def test_invalid_unknown_and_handler_errors_are_safe(self):
        async def broken(context, arguments):
            raise RuntimeError("secret")

        for name, arguments, code in (("missing", "{}", "provider_requested_unexposed_tool"), ("test_tool", "not-json", "invalid_arguments"), ("test_tool", "{}", "tool_execution_error")):
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

    def test_tool_call_limit_and_output_limit(self):
        async def handler(context, arguments):
            return {"value": "x" * 1000}

        responses = [[{"tool_calls": [{"index": 0, "id": str(i), "function": {"name": "test_tool", "arguments": "{}"}}]}] for i in range(4)]
        responses.append([{"content": "Synthesized from collected results."}])
        adapter = Adapter(responses)
        registry = self.registry(handler)
        events = run(AgentRuntime(AgentRuntimeLimits(3, 100)), adapter, registry)
        self.assertEqual(len(adapter.payloads), 5)
        self.assertEqual(events[-1]["answer"], "Synthesized from collected results.")
        self.assertEqual(len([payload for payload in adapter.payloads if payload.get("tools")]), 3)
        tool_message = next(message for message in adapter.payloads[1]["messages"] if message.get("role") == "tool")
        self.assertLessEqual(len(tool_message["content"]), 100)

    def test_artifact_errors_are_traced_and_rendering_stops_after_one_retry(self):
        calls = []

        async def render(_context, arguments):
            calls.append(arguments)
            return {"error": {"code": "invalid_artifact", "message": "El gráfico requiere labels y series."}}

        registry = ModuleRegistry(FastAPI())
        registry.context.tools.register(ToolDefinition("native.render_artifact", "render", {"type": "object"}, render, action="read_only"))
        responses = [[{"tool_calls": [{"index": 0, "id": str(i), "function": {"name": "native.render_artifact", "arguments": "{}"}}]}] for i in range(3)]
        responses.append([{"content": "No se pudo crear el gráfico por datos inválidos; aquí están los datos."}])
        adapter = Adapter(responses)
        events = run(AgentRuntime(), adapter, registry)

        self.assertEqual(len(calls), 2)
        self.assertEqual([event["trace"]["status"] for event in events if event.get("trace", {}).get("type") == "ACT"], ["invalid_artifact", "invalid_artifact", "artifact_retry_limit"])
        self.assertIn("artifact_retry_limit", adapter.payloads[3]["messages"][-1]["content"])
        guidance = next(message["content"] for message in adapter.payloads[0]["messages"] if message["role"] == "system")
        self.assertIn("at most once", guidance)

    def test_successful_artifact_renders_do_not_consume_retry_budget(self):
        calls = []

        async def render(_context, arguments):
            calls.append(arguments)
            return {"artifacts": [{"type": "bar", "title": arguments["title"], "data": {}}]}

        registry = ModuleRegistry(FastAPI())
        registry.context.tools.register(ToolDefinition("native.render_artifact", "render", {"type": "object"}, render, action="read_only"))
        responses = [[{"tool_calls": [{"index": 0, "id": str(i), "function": {"name": "native.render_artifact", "arguments": json.dumps({"title": str(i)})}}]}] for i in range(3)]
        responses.append([{"content": "Generated three charts."}])
        adapter = Adapter(responses)
        events = run(AgentRuntime(), adapter, registry)

        self.assertEqual(len(calls), 3)
        self.assertEqual(len(events[-1]["artifacts"]), 3)
        self.assertNotIn("artifact_retry_limit", json.dumps(events))

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
