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
    def __init__(self, response, calls=None):
        self.response = response
        self.payload = None
        self.headers = None
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        self.payload = json
        self.headers = headers
        if self.calls is not None:
            self.calls.append(("POST", url, headers, json))
        return self.response

    async def get(self, url, headers):
        if self.calls is not None:
            self.calls.append(("GET", url, headers))
        return self.response


class FakeResponse:
    def __init__(self, payload=None, status=200, json_error=False):
        self.payload = payload
        self.status = status
        self.json_error = json_error

    @property
    def is_success(self):
        return 200 <= self.status < 300

    def raise_for_status(self):
        if not self.is_success:
            import httpx

            request = httpx.Request("POST", "http://arbiter/v1/systemone")
            raise httpx.HTTPStatusError("error", request=request, response=httpx.Response(self.status, request=request))

    def json(self):
        if self.json_error:
            raise ValueError("malformed json")
        return self.payload


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

    def test_arbiter_uses_readyz_and_authentication(self):
        calls = []
        response = FakeResponse({"answers": {}})
        provider = ArbiterDecisionProvider("http://arbiter:8000", "secret", 1, client_factory=lambda **_: FakeHttpClient(response, calls))
        self.assertTrue(asyncio.run(provider.check_available()))
        self.assertEqual(calls[0][0:2], ("GET", "http://arbiter:8000/readyz"))
        self.assertEqual(calls[0][2], {"Authorization": "Bearer secret"})
        self.assertNotIn("/health", calls[0][1])

    def test_arbiter_readiness_failure_is_non_blocking(self):
        response = FakeResponse(status=503)
        provider = ArbiterDecisionProvider("http://arbiter:8000", "", 1, client_factory=lambda **_: FakeHttpClient(response))
        self.assertFalse(asyncio.run(provider.check_available()))

    def test_system_one_endpoint_accepts_base_url_with_or_without_v1(self):
        for base, expected in (
            ("http://arbiter:8000", "http://arbiter:8000/v1/systemone"),
            ("http://arbiter:8000/v1", "http://arbiter:8000/v1/systemone"),
        ):
            calls = []
            response = FakeResponse({"answers": {"x": {"type": "noul", "noul": 0.9}}})
            provider = ArbiterDecisionProvider(base, "", 1, client_factory=lambda **_: FakeHttpClient(response, calls))
            asyncio.run(provider.decide(request({"id": "x", "type": "boolean", "statement": "x"})))
            self.assertEqual(calls[0][1], expected)

    def test_default_model_is_sent_unchanged_and_override_is_opaque(self):
        for model in ("jev-latest", "laya-typed-decisions"):
            calls = []
            response = FakeResponse({"answers": {"x": {"type": "noul", "noul": 0.9}}})
            provider = ArbiterDecisionProvider("http://arbiter:8000", "", 1, model=model, client_factory=lambda **_: FakeHttpClient(response, calls))
            asyncio.run(provider.decide(request({"id": "x", "type": "boolean", "statement": "x"})))
            self.assertEqual(calls[0][3]["model"], model)

    def test_boolean_noul_normalizes_both_sides(self):
        req = request({"id": "true_case", "type": "boolean", "statement": "x"}, {"id": "false_case", "type": "boolean", "statement": "y"})
        parsed = ArbiterDecisionProvider._parse_response({"answers": {"true_case": {"type": "noul", "noul": 0.9}, "false_case": {"type": "noul", "noul": 0.2}}}, req)
        self.assertEqual(parsed.answers["true_case"].model_dump(), {"type": "boolean", "value": True, "probabilities": {"true": 0.9, "false": 0.1}, "confidence": 0.9})
        self.assertFalse(parsed.answers["false_case"].value)
        self.assertEqual(parsed.answers["false_case"].confidence, 0.8)

    def test_choice_and_score_payloads_and_probabilities_are_preserved(self):
        req = request(
            {"id": "task_type", "type": "choice", "statement": "Task", "options": ["research", "coding"]},
            {"id": "complexity", "type": "score", "statement": "Complexity", "scale": ["simple", "complex"]},
        )
        raw = {"answers": {"task_type": {"type": "choice", "choice": "research", "probabilities": {"research": 0.8, "coding": 0.2}, "confidence": 0.8}, "complexity": {"type": "score", "score": "complex", "probabilities": {"simple": 0.1, "complex": 0.9}, "confidence": 0.9}}}
        parsed = ArbiterDecisionProvider._parse_response(raw, req)
        self.assertEqual(parsed.answers["task_type"].probabilities, {"research": 0.8, "coding": 0.2})
        self.assertEqual(parsed.answers["complexity"].value, "complex")
        self.assertEqual(parsed.answers["complexity"].probabilities["complex"], 0.9)

        calls = []
        provider = ArbiterDecisionProvider("http://arbiter:8000", "", 1, client_factory=lambda **_: FakeHttpClient(FakeResponse(raw), calls))
        asyncio.run(provider.decide(req))
        questions = calls[0][3]["questions"]
        self.assertEqual(questions["task_type"]["criteria"], ["research", "coding"])
        self.assertEqual(questions["complexity"]["criteria"], ["simple", "complex"])

    def test_metadata_is_safe_optional_and_unknown_fields_are_ignored(self):
        req = request({"id": "x", "type": "boolean", "statement": "x"})
        parsed = ArbiterDecisionProvider._parse_response({"model": "jev-latest", "routing": "research", "latency_ms": 32, "foo": "bar", "diagnostics": {"internal": "ignored"}, "answers": {"x": {"type": "noul", "noul": 0.9}}}, req)
        self.assertEqual(parsed.metadata, {"routing": "research", "latency_ms": 32})
        self.assertTrue(parsed.answers["x"].value)

    def test_malformed_json_is_invalid_response(self):
        response = FakeResponse(json_error=True)
        provider = ArbiterDecisionProvider("http://arbiter:8000", "", 1, client_factory=lambda **_: FakeHttpClient(response))
        with self.assertRaisesRegex(ArbiterError, "decision_invalid_response"):
            asyncio.run(provider.decide(request({"id": "x", "type": "boolean", "statement": "x"})))

    def test_multi_question_request_is_one_system_one_call(self):
        calls = []
        response = FakeResponse({"model": "jev-latest", "answers": {"needs_current_information": {"type": "noul", "noul": 0.9}, "task_type": {"type": "choice", "choice": "research", "probabilities": {"research": 0.9, "coding": 0.05, "conversation": 0.03, "analysis": 0.02}, "confidence": 0.9}, "complexity": {"type": "score", "score": "moderate", "probabilities": {"trivial": 0.01, "simple": 0.1, "moderate": 0.7, "complex": 0.15, "very_complex": 0.04}, "confidence": 0.7}}})
        provider = ArbiterDecisionProvider("http://arbiter:8000", "", 1, client_factory=lambda **_: FakeHttpClient(response, calls))
        result = asyncio.run(provider.decide(DecisionRequest(state="The user asks for the latest Qwen model news and wants a comparison.", questions=[{"id": "needs_current_information", "type": "boolean", "statement": "Does this need current information?"}, {"id": "task_type", "type": "choice", "statement": "What kind of task is this?", "options": ["research", "coding", "conversation", "analysis"]}, {"id": "complexity", "type": "score", "statement": "How complex is this?", "scale": ["trivial", "simple", "moderate", "complex", "very_complex"]}])))
        self.assertEqual(len([call for call in calls if call[0] == "POST"]), 1)
        self.assertEqual(set(result.answers), {"needs_current_information", "task_type", "complexity"})

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
