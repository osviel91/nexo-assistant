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
    def test_action_policy_allows_reads_and_requires_approval_for_other_actions(self):
        evaluator = PolicyEvaluator()
        for action, decision in (("read_only", PolicyDecision.ALLOW), ("mutating", PolicyDecision.APPROVAL_REQUIRED), ("destructive", PolicyDecision.APPROVAL_REQUIRED), (None, PolicyDecision.APPROVAL_REQUIRED)):
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

    def test_unknown_action_is_blocked_with_real_settings_path(self):
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
        self.assertIn("tool_policy_blocked", response)
        self.assertIn("Settings > Tools", response)
        self.assertNotIn("run_id", response)
        self.assertIn("no interactive approval flow", response)
        self.assertEqual(events[-1]["answer"], "approval needed")


if __name__ == "__main__":
    unittest.main()
