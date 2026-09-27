import asyncio
import unittest
from types import SimpleNamespace

from fastapi import FastAPI

from app.kernel import ModuleContext, ToolExecutionContext, ToolRegistry
from app.modules.mcp import MCPModule, namespace, parse_server_config


class FakeClient:
    def __init__(self, tools, result=None, error=False, delay=0):
        self.tools = tools
        self.result = result or SimpleNamespace(content=[SimpleNamespace(text="ok")], is_error=error)
        self.delay = delay
        self.calls = []
        self.entered = False
        self.closed = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


def tool(name, schema=None):
    return SimpleNamespace(name=name, description=name, inputSchema=schema or {"type": "object"})


class MCPTests(unittest.TestCase):
    def context(self, registry, config):
        return ModuleContext(FastAPI(), {"mcp_servers": config}, registry)

    def start(self, module, context):
        module.register(context)
        module.startup(context)

    def test_disabled_server_does_not_connect(self):
        called = []
        module = MCPModule(lambda url: called.append(url))
        self.start(module, self.context(ToolRegistry(), [{"id": "one", "url": "http://one", "enabled": False, "allowed_tools": ["x"]}]))
        self.assertEqual(called, [])
        self.assertEqual(module.catalog_status()["servers"][0]["exposed_tools"], 0)

    def test_allowlist_and_empty_allowlist(self):
        clients = {}

        def factory(url):
            client = FakeClient([tool("read"), tool("hidden")])
            clients[url] = client
            return client

        registry = ToolRegistry()
        module = MCPModule(factory)
        self.start(module, self.context(registry, [{"id": "files", "url": "http://files", "allowed_tools": ["read"]}]))
        self.assertEqual([d["function"]["name"] for d in registry.definitions()], ["mcp__files__read"])
        self.assertEqual(clients["http://files"].entered, True)

        empty = ToolRegistry()
        self.start(MCPModule(lambda url: FakeClient([tool("read")])), self.context(empty, [{"id": "empty", "url": "http://empty", "allowed_tools": []}]))
        self.assertEqual(empty.definitions(), [])

    def test_namespaces_and_executes_call(self):
        clients = [FakeClient([tool("search")]), FakeClient([tool("search")])]
        module = MCPModule(lambda url: clients.pop(0))
        registry = ToolRegistry()
        context = self.context(registry, [
            {"id": "one", "url": "http://one", "allowed_tools": ["search"]},
            {"id": "two", "url": "http://two", "allowed_tools": ["search"]},
        ])
        self.start(module, context)
        self.assertEqual({d["function"]["name"] for d in registry.definitions()}, {"mcp__one__search", "mcp__two__search"})
        result = asyncio.run(registry.invoke("mcp__one__search", ToolExecutionContext("c", "p", "m", 1), {"q": "x"}))
        self.assertEqual(result, {"content": "ok"})

    def test_timeout_and_bad_schema_are_safe(self):
        client = FakeClient([tool("bad", {"type": "string"}), tool("slow")], delay=0.2)
        registry = ToolRegistry()
        module = MCPModule(lambda url: client)
        self.start(module, self.context(registry, [{"id": "one", "url": "http://one", "allowed_tools": ["bad", "slow"], "timeout": 0.1}]))
        self.assertEqual([d["function"]["name"] for d in registry.definitions()], ["mcp__one__slow"])
        result = asyncio.run(registry.invoke("mcp__one__slow", ToolExecutionContext("c", "p", "m", 1), {}))
        self.assertEqual(result["error"]["code"], "tool_timeout")

    def test_connection_failure_does_not_block_registry(self):
        class Broken:
            async def __aenter__(self):
                raise OSError("offline")

        registry = ToolRegistry()
        module = MCPModule(lambda url: Broken())
        self.start(module, self.context(registry, [{"id": "down", "url": "http://down", "allowed_tools": ["x"]}]))
        status = module.catalog_status()["servers"][0]
        self.assertFalse(status["connected"])
        self.assertEqual(status["last_error"], "mcp_connection_error")
        self.assertEqual(registry.definitions(), [])

    def test_config_and_namespace_are_deterministic(self):
        config = parse_server_config('[{"id":"a","url":"https://example.test/mcp","allowed_tools":["x"]}]')
        self.assertEqual(config[0]["id"], "a")
        self.assertEqual(namespace("a", "x"), "mcp__a__x")
        self.assertEqual(parse_server_config('[{"id":"bad id","url":"http://x"}]'), [])


if __name__ == "__main__":
    unittest.main()
