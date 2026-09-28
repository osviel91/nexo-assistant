import unittest
from unittest.mock import patch

from app.decision.models import DecisionRequest
from app.shadow_evaluation import WORDING_VARIANTS, evaluation_request, latency_summary, score_cases


class ShadowEvaluationTests(unittest.TestCase):
    def test_baseline_wording_matches_production_and_candidates_only_change_booleans(self):
        baseline = evaluation_request("x", ["web_search"], "baseline")
        self.assertEqual(baseline.questions[0].statement, "Would this request materially benefit from current or external information obtained through web search?")
        self.assertEqual(baseline.questions[1].statement, "Would solving this request materially benefit from using one of the available agent tools?")
        self.assertEqual(baseline.questions[2].statement, "What is the primary type of this request?")
        for variant in ("candidate-a", "candidate-b"):
            request = evaluation_request("x", ["web_search"], variant)
            self.assertNotEqual(request.questions[0].statement, baseline.questions[0].statement)
            self.assertNotEqual(request.questions[1].statement, baseline.questions[1].statement)
            self.assertEqual(request.questions[2].statement, baseline.questions[2].statement)
        self.assertEqual(set(WORDING_VARIANTS), {"baseline", "candidate-a", "candidate-b"})

    def test_evaluation_runtime_override_sets_only_provider_model(self):
        from app.shadow_evaluation import evaluation_runtime

        class Response:
            def raise_for_status(self): pass
            def json(self): return {"answers": {"check": {"type": "noul", "noul": 0.9}}, "model": "laya-english"}

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, endpoint, headers, json):
                self.payload = json
                return Response()

        client = Client()
        runtime = evaluation_runtime("http://arbiter", "", 1, "laya-english")
        runtime.provider.client_factory = lambda **kwargs: client
        with patch.object(client, "post", wraps=client.post) as post:
            import asyncio
            asyncio.run(runtime.decide(DecisionRequest(state={"message": "x"}, questions=[{"id": "check", "type": "boolean", "statement": "x"}])))
        self.assertEqual(post.call_args.kwargs["json"]["model"], "laya-english")

    def test_scores_booleans_tasks_and_confidence(self):
        cases = [{"id": "web", "expected": {"needs_web": True}}, {"id": "code", "expected": {"task_type": "coding"}}]
        results = [{"case_id": "web", "answers": {"needs_web": {"value": True, "confidence": .9}}}, {"case_id": "code", "answers": {"task_type": {"value": "analysis", "confidence": .8}}}]
        metrics = score_cases(cases, results)
        self.assertEqual(metrics["boolean"]["needs_web"]["correct"], 1)
        self.assertEqual(metrics["task_type"]["confusion"]["coding"]["analysis"], 1)
        self.assertEqual(metrics["confidence"]["correct"], .9)

    def test_latency_summary(self):
        self.assertEqual(latency_summary([{"latency_ms": value} for value in (10, 20, 30, 40)]), {"min": 10.0, "p50": 20.0, "p95": 30.0, "max": 40.0})


if __name__ == "__main__":
    unittest.main()
