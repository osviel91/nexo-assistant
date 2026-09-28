import unittest

from app.capabilities import normalize_model_capabilities


class CapabilityTests(unittest.TestCase):
    def test_normalizes_supported_tool_shapes(self):
        self.assertIn("tool-calling", normalize_model_capabilities({"capabilities": ["tool-calling"]}))
        self.assertIn("tool-calling", normalize_model_capabilities({"supports_tools": True}))
        self.assertIn("tool-calling", normalize_model_capabilities({"tool_calling": True}))
        self.assertIn("tool-calling", normalize_model_capabilities({"supported_parameters": ["tools"]}))

    def test_unknown_capabilities_require_explicit_fallback(self):
        self.assertNotIn("tool-calling", normalize_model_capabilities({}))
        self.assertIn("tool-calling", normalize_model_capabilities({}, assume_tool_calling=True))


if __name__ == "__main__":
    unittest.main()
