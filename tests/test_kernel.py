import unittest

from fastapi import FastAPI

from app.kernel import InterfaceExtension, ModuleContext, ModuleManifest, ModuleRegistry


class FakeModule:
    def __init__(self, module_id="fake", api_version=1, dependencies=(), fail=None):
        self.manifest = ModuleManifest(module_id, module_id, "1.0.0", api_version, dependencies=dependencies)
        self.interface_extensions = (InterfaceExtension("fake-control", "test", "Fake"),)
        self.fail = fail
        self.started = False
        self.hooks = 0

    def register(self, context):
        if self.fail == "register":
            raise RuntimeError("register")

    def startup(self, context):
        if self.fail == "startup":
            raise RuntimeError("startup")
        self.started = True

    def shutdown(self, context):
        return None

    def run_hook(self, hook, context, payload):
        if self.fail == "hook":
            raise RuntimeError("hook")
        self.hooks += 1


class KernelTests(unittest.TestCase):
    def registry(self):
        return ModuleRegistry(FastAPI())

    def test_registers_catalog_and_lifecycle(self):
        registry = self.registry()
        module = FakeModule()
        self.assertTrue(registry.register(module))
        registry.startup()
        registry.run_hook("chat_before", {})
        self.assertTrue(module.started)
        self.assertEqual(module.hooks, 1)
        self.assertEqual(registry.catalog()[0]["id"], "fake")

    def test_disabled_module_is_not_registered(self):
        registry = self.registry()
        self.assertEqual(registry.catalog(), [])

    def test_rejects_incompatible_api_and_missing_dependency(self):
        registry = self.registry()
        self.assertFalse(registry.register(FakeModule(api_version=99)))
        self.assertFalse(registry.register(FakeModule(dependencies=("missing",))))
        self.assertEqual(registry.catalog(), [])

    def test_extension_failures_are_isolated(self):
        registry = self.registry()
        broken = FakeModule("broken", fail="startup")
        healthy = FakeModule("healthy")
        hooked = FakeModule("hooked", fail="hook")
        self.assertTrue(registry.register(broken))
        self.assertTrue(registry.register(healthy))
        self.assertTrue(registry.register(hooked))
        registry.startup()
        registry.run_hook("chat_after", {})
        self.assertTrue(healthy.started)
        self.assertEqual(healthy.hooks, 1)
        self.assertTrue(any("broken" in item for item in registry.diagnostics))
        self.assertTrue(any("hooked" in item for item in registry.diagnostics))


if __name__ == "__main__":
    unittest.main()
