import asyncio
import unittest
from types import SimpleNamespace

from fastapi import FastAPI

from app.agent import AgentRunRequest, AgentRuntime
from app.agent_model import ModelStreamChunk
from app.kernel import ModuleContext, ModuleRegistry, ToolDefinition, ToolExecutionContext, ToolRegistry
from app.modules.mcp import MCPModule
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
    def native_registry(self, seen):
        registry = ModuleRegistry(FastAPI())

        async def handler(_context, arguments):
            seen.append(arguments)
            return {"ok": True}

        registry.context.tools.register(ToolDefinition("visible", "Visible", {"type": "object"}, handler))
        registry.context.tools.register(ToolDefinition("hidden", "Hidden", {"type": "object"}, handler))
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

    def test_mcp_tool_uses_the_same_policy_and_executor(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def list_tools(self):
                return SimpleNamespace(tools=[
                    SimpleNamespace(name="visible", description="Visible", inputSchema={"type": "object"}),
                    SimpleNamespace(name="hidden", description="Hidden", inputSchema={"type": "object"}),
                ])

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                return SimpleNamespace(content=[SimpleNamespace(text="ok")], is_error=False)

        client = Client()
        registry = ToolRegistry()
        module = MCPModule(lambda _url: client)
        context = ModuleContext(FastAPI(), {"mcp_servers": [{"id": "server", "url": "http://server", "allowed_tools": ["visible", "hidden"]}]}, registry)
        module.register(context)
        module.startup(context)
        async def run(name):
            adapter = Adapter([
                [{"tool_calls": [{"id": "call", "function": {"name": name, "arguments": "{}"}}]}],
                [{"content": "continued"}],
            ])
            execution_context = ToolExecutionContext("conversation", "provider", "model", 0)
            effective = ExposurePolicy().resolve(registry.catalog_view(), {"tool-calling"}, {"mcp__server__visible"})
            request = AgentRunRequest(adapter, [{"role": "user", "content": "x"}], effective, ToolExecutor(), execution_context)
            return adapter, [event async for event in AgentRuntime().stream(request)]

        hidden_adapter, hidden_events = asyncio.run(run("mcp__server__hidden"))
        self.assertIn("tool_not_available", hidden_adapter.payloads[1]["messages"][-1]["content"])
        self.assertEqual(client.calls, [])
        adapter, events = asyncio.run(run("mcp__server__visible"))
        self.assertEqual(client.calls, [("visible", {})])
        self.assertEqual(events[-1]["answer"], "continued")


if __name__ == "__main__":
    unittest.main()
