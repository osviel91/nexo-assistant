import unittest

from app.shadow_evaluation import latency_summary, score_cases


class ShadowEvaluationTests(unittest.TestCase):
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
