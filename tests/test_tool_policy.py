import asyncio
import unittest
from types import SimpleNamespace

from fastapi import FastAPI

from app.agent import AgentRunRequest, AgentRuntime, PolicyDecision, PolicyEvaluator
from app.agent_model import ModelStreamChunk
from app.kernel import ModuleContext, ModuleRegistry, ToolDefinition, ToolExecutionContext, ToolRegistry
from app.tools import ExposurePolicy, ToolExecutor


class Adapter:
    model_id = "model"

    def __init__(self, calls):
        self.calls = iter(calls)
        self.payloads = []

    async def stream(self, messages, tools, temperature=None):
        self.payloads.append({"messages": messages, "tools": tools})
        for chunk in next(self.calls):
            yield ModelStreamChunk(content=chunk.get("content", ""), tool_calls=chunk.get("tool_calls"))


def execute(registry, adapter, capabilities={"tool-calling"}, requested=None):
    async def collect():
        context = ToolExecutionContext("conversation", "provider", "model", 0)
        effective = ExposurePolicy().resolve(registry.tool_catalog_view(), capabilities, requested)
        request = AgentRunRequest(adapter, [{"role": "user", "content": "x"}], effective, ToolExecutor(), context)
        return [event async for event in AgentRuntime().stream(request)]

    return asyncio.run(collect())


class ToolPolicyTests(unittest.TestCase):
    def test_action_policy_allows_reads_approves_mutations_and_denies_unknown(self):
        evaluator = PolicyEvaluator()
        for action, decision in (("read_only", PolicyDecision.ALLOW), ("mutating", PolicyDecision.APPROVAL_REQUIRED), ("destructive", PolicyDecision.APPROVAL_REQUIRED), ("unknown", PolicyDecision.DENY), (None, PolicyDecision.DENY)):
            tool = SimpleNamespace(action=action)
            self.assertEqual(evaluator.evaluate(tool), decision)

    def native_registry(self, seen):
        registry = ModuleRegistry(FastAPI())

        async def handler(_context, arguments):
            seen.append(arguments)
            return {"ok": True}

        registry.context.tools.register(ToolDefinition("visible", "Visible", {"type": "object"}, handler, action="read_only"))
        registry.context.tools.register(ToolDefinition("hidden", "Hidden", {"type": "object"}, handler, action="read_only"))
        return registry

    def test_catalog_is_safe_and_policy_resolves_requested_tools(self):
        registry = self.native_registry([])
        catalog = registry.tool_catalog_view()
        self.assertEqual({entry.name for entry in catalog.entries()}, {"visible", "hidden"})
        self.assertEqual(catalog.entries()[0].source, "native")
        effective = ExposurePolicy().resolve(catalog, {"tool-calling"}, {"visible"})
        self.assertEqual(effective.names, {"visible"})
        self.assertEqual([tool["function"]["name"] for tool in effective.definitions()], ["visible"])

    def test_exposed_native_tool_executes_and_continues(self):
        seen = []
        registry = self.native_registry(seen)
        adapter = Adapter([
            [{"tool_calls": [{"id": "call", "function": {"name": "visible", "arguments": "{}"}}]}],
            [{"content": "continued"}],
        ])
        events = execute(registry, adapter, requested={"visible"})
        self.assertEqual(seen, [{}])
        self.assertEqual(events[-1]["answer"], "continued")
        self.assertEqual(adapter.payloads[0]["tools"][0]["function"]["name"], "visible")

    def test_known_unexposed_and_nonexistent_calls_never_execute(self):
        seen = []
        registry = self.native_registry(seen)
        for name in ("hidden", "fabricated"):
            adapter = Adapter([
                [{"tool_calls": [{"id": "call", "function": {"name": name, "arguments": "{\"secret\":\"value\"}"}}]}],
                [{"content": "continued"}],
            ])
            events = execute(registry, adapter, requested={"visible"})
            self.assertEqual(seen, [])
            self.assertIn("tool_not_available", adapter.payloads[1]["messages"][-1]["content"])
            self.assertNotIn("secret", str([event.get("trace") for event in events if "trace" in event]))

    def test_model_without_tool_support_receives_no_definitions(self):
        registry = self.native_registry([])
        adapter = Adapter([[{"content": "plain"}]])
        execute(registry, adapter, set())
        self.assertEqual(adapter.payloads[0]["tools"], [])

    def test_unknown_action_is_blocked_without_approval(self):
        seen = []
        registry = self.native_registry(seen)
        tool = registry.context.tools._tools["visible"]
        registry.context.tools._tools["visible"] = ToolDefinition(tool.name, tool.description, tool.parameters, tool.handler)
        adapter = Adapter([
            [{"tool_calls": [{"id": "call", "function": {"name": "visible", "arguments": "{}"}}]}],
            [{"content": "approval needed"}],
        ])
        events = execute(registry, adapter, requested={"visible"})
        self.assertEqual(seen, [])
        response = adapter.payloads[1]["messages"][-1]["content"]
        self.assertIn("unclassified_tool", response)
        self.assertEqual(events[-1]["answer"], "approval needed")

    def test_empty_provider_response_is_an_error_not_success(self):
        registry = self.native_registry([])
        events = execute(registry, Adapter([[{"content": ""}]]))
        self.assertEqual(events[-1], {"error": "provider_empty_response"})

    def test_approval_freezes_exact_call_and_pauses_without_execution(self):
        seen = []
        registry = ModuleRegistry(FastAPI())

        async def handler(_context, arguments):
            seen.append(arguments)
            return {"ok": True}

        registry.context.tools.register(ToolDefinition("vault.write", "Write a note", {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False,
        }, handler, source="mcp", module_id="mcp", action="mutating"))
        adapter = Adapter([
            [{"tool_calls": [{"id": "call", "function": {"name": "vault.write", "arguments": '{"path":"initial.md"}'}}]}],
        ])

        async def collect():
            effective = ExposurePolicy().resolve(registry.tool_catalog_view(), {"tool-calling"})
            request = AgentRunRequest(adapter, [{"role": "user", "content": "write"}], effective, ToolExecutor(), ToolExecutionContext("conversation", "provider", "model", 0), interaction_handler=True)
            events = [event async for event in AgentRuntime().stream(request)]
            return events

        events = asyncio.run(collect())
        pending = next(event["approval_required"] for event in events if "approval_required" in event)
        self.assertEqual(pending["arguments"], {"path": "initial.md"})
        self.assertEqual(pending["tool_call_id"], "call")
        self.assertEqual(pending["continuation"]["tool_call_count"], 1)
        self.assertEqual(pending["continuation"]["messages"][-1]["tool_calls"][0]["id"], "call")
        self.assertEqual(seen, [])

    def test_resumed_run_preserves_provider_tool_call_and_budget(self):
        seen = []
        registry = ModuleRegistry(FastAPI())

        async def handler(_context, arguments):
            seen.append(arguments)
            return {"ok": True}

        registry.context.tools.register(ToolDefinition("vault.write", "Write", {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False,
        }, handler, source="mcp", module_id="mcp", action="mutating"))
        adapter = Adapter([
            [{"content": "finished"}],
        ])

        async def collect():
            effective = ExposurePolicy().resolve(registry.tool_catalog_view(), {"tool-calling"})
            checkpoint = [{"role": "assistant", "tool_calls": [{"id": "first", "type": "function", "function": {"name": "vault.write", "arguments": '{"path":"one.md"}'}}]},
                          {"role": "tool", "tool_call_id": "first", "name": "vault.write", "content": '{"ok":true}'}]
            request = AgentRunRequest(adapter, [], effective, ToolExecutor(), ToolExecutionContext("conversation", "provider", "model", 1),
                                      resume_messages=checkpoint, initial_tool_call_count=1, initial_tool_rounds=1, initial_tools_used=["vault.write"])
            events = [event async for event in AgentRuntime().stream(request)]
            return events

        events = asyncio.run(collect())
        self.assertEqual(adapter.payloads[0]["messages"][:2], [{"role": "assistant", "tool_calls": [{"id": "first", "type": "function", "function": {"name": "vault.write", "arguments": '{"path":"one.md"}'}}]}, {"role": "tool", "tool_call_id": "first", "name": "vault.write", "content": '{"ok":true}'}])
        self.assertEqual(seen, [])
        self.assertEqual(events[-1]["answer"], "finished")

    def test_agent_can_request_structured_user_input(self):
        registry = ModuleRegistry(FastAPI())
        from app.native_tools import register_native_tools
        register_native_tools(registry.context)
        adapter = Adapter([
            [{"tool_calls": [{"id": "ask", "function": {"name": "native.request_user_input", "arguments": '{"dialog":{"title":"Choose","fields":[{"id":"choice","label":"Choose","type":"select","required":true,"options":["A","B"]}]}}'}}]}],
            [{"content": "selected"}],
        ])

        async def collect():
            effective = ExposurePolicy().resolve(registry.tool_catalog_view(), {"tool-calling"})
            request = AgentRunRequest(adapter, [{"role": "user", "content": "choose"}], effective, ToolExecutor(), ToolExecutionContext("conversation", "provider", "model", 0), interaction_handler=True)
            stream = AgentRuntime().stream(request)
            events = []
            event = await stream.__anext__()
            while True:
                if "interaction" in event:
                    events.append(event)
                    self.assertEqual(event["interaction"]["kind"], "user_input")
                    event = await stream.asend({"approved": True, "values": {"choice": "B"}})
                else:
                    events.append(event)
                    try:
                        event = await stream.__anext__()
                    except StopAsyncIteration:
                        return events

        events = asyncio.run(collect())
        self.assertIn('"choice":"B"', adapter.payloads[1]["messages"][-1]["content"])
        self.assertEqual(events[-1]["answer"], "selected")


if __name__ == "__main__":
    unittest.main()
