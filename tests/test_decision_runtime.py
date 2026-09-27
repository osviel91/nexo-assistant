import asyncio
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.decision.models import DecisionRequest
from app.decision.providers.arbiter import ArbiterDecisionProvider, ArbiterError
from app.decision.runtime import DecisionRuntime, DecisionRuntimeError
from app.kernel import ModuleRegistry
from app.modules.decision_runtime import DecisionRuntimeModule


def request(*questions):
    return DecisionRequest(state="current state", questions=list(questions))


class FakeProvider:
    name = "fake"

    def __init__(self, result=None, error=None, available=True):
        self.result = result
        self.error = error
        self.available = available
        self.calls = 0

    async def decide(self, value):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def check_available(self):
        return self.available


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.payload = None
        self.headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        self.payload = json
        self.headers = headers
        return self.response

    async def get(self, url, headers):
        return self.response


class DecisionRuntimeTests(unittest.TestCase):
    def test_disabled_registry_has_no_decision_service_or_route(self):
        app = FastAPI()
        registry = ModuleRegistry(app)
        self.assertIsNone(registry.service("decision"))
        self.assertFalse(any(route.path == "/api/decisions" for route in app.routes))

    def test_enabled_module_registers_runtime_and_reports_unavailable(self):
        app = FastAPI()
        registry = ModuleRegistry(app)
        provider = FakeProvider(available=False)
        module = DecisionRuntimeModule(provider=provider)
        self.assertTrue(registry.register(module))
        asyncio.run(module.startup_async())
        self.assertIs(registry.service("decision"), module.runtime)
        self.assertEqual(module.catalog_status()["last_error"], "decision_provider_unavailable")

    def test_runtime_preserves_typed_result(self):
        question = {"id": "needs_web", "type": "boolean", "statement": "Needs web"}
        result = {"answers": {"needs_web": {"type": "boolean", "value": True}}}
        from app.decision.models import DecisionResult

        provider = FakeProvider(DecisionResult.model_validate(result))
        output = asyncio.run(DecisionRuntime(provider, 1).decide(request(question)))
        self.assertTrue(output.answers["needs_web"].value)

    def test_runtime_timeout_is_typed(self):
        class SlowProvider(FakeProvider):
            async def decide(self, value):
                await asyncio.sleep(0.05)

        with self.assertRaisesRegex(DecisionRuntimeError, "decision_timeout"):
            asyncio.run(DecisionRuntime(SlowProvider(), 0.001).decide(request({"id": "x", "type": "boolean", "statement": "x"})))

    def test_invalid_request_is_rejected_before_provider(self):
        with self.assertRaises(ValueError):
            DecisionRequest(state="x", questions=[{"id": "x", "type": "choice", "statement": "x", "options": {"only": "one"}}])

    def test_arbiter_payload_and_response_normalization(self):
        questions = [
            {"id": "flag", "type": "boolean", "statement": "Is it true?"},
            {"id": "kind", "type": "choice", "statement": "Which?", "options": {"a": "A", "b": "B"}},
            {"id": "risk", "type": "score", "statement": "How risky?", "scale": ["low", "high"]},
        ]
        req = request(*questions)
        raw = {"model": "jev-1", "answers": {
            "flag": {"type": "noul", "noul": 0.9},
            "kind": {"type": "choice", "choice": "b", "probabilities": {"a": 0.2, "b": 0.8}, "confidence": 0.7},
            "risk": {"type": "score", "score": 0.8, "probabilities": {"0": 0.2, "1": 0.8}, "confidence": 0.6},
        }}
        parsed = ArbiterDecisionProvider._parse_response(raw, req)
        self.assertTrue(parsed.answers["flag"].value)
        self.assertEqual(parsed.answers["kind"].value, "b")
        self.assertEqual(parsed.answers["risk"].probabilities["1"], 0.8)

    def test_invalid_arbiter_response_isolated(self):
        req = request({"id": "x", "type": "boolean", "statement": "x"})
        with self.assertRaisesRegex(ArbiterError, "decision_invalid_response"):
            ArbiterDecisionProvider._parse_response({"answers": {"x": {"type": "noul"}}}, req)

    def test_endpoint_does_not_expose_provider_secret(self):
        app = FastAPI()
        provider = FakeProvider(available=True)
        module = DecisionRuntimeModule(provider=provider)
        registry = ModuleRegistry(app)
        registry.register(module)
        with TestClient(app) as client:
            response = client.post("/api/decisions", json={"state": "x", "questions": [{"id": "x", "type": "boolean", "statement": "x"}]})
        self.assertNotIn("api-key", response.text)


if __name__ == "__main__":
    unittest.main()
