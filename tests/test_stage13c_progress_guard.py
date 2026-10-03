import time
import unittest

from app.progress_guard import ProgressGuard, call_fingerprint


def result(guard, name, arguments, output, status="ok"):
    identity = call_fingerprint(name, arguments)
    guard.observe_call(identity)
    return guard.observe_result(output, status, 1)


class ProgressGuardTests(unittest.TestCase):
    def test_canonical_arguments_ignore_object_order_only(self):
        self.assertEqual(call_fingerprint("search", {"a": 1, "b": "home lab"}),
                         call_fingerprint("search", {"b": "home lab", "a": 1}))
        self.assertNotEqual(call_fingerprint("search", {"q": "home  lab"}), call_fingerprint("search", {"q": "home lab"}))

    def test_productive_vault_research_and_pagination_decay_stagnation(self):
        guard = ProgressGuard()
        workflow = [
            ("search", {"query": "homelab"}, {"ids": ["folder-a", "folder-b"]}),
            ("list", {"folder": "architecture"}, {"ids": ["wrong"]}),
            ("search", {"query": "infra"}, {"ids": ["infra"]}),
            ("read", {"path": "Infra/MOC"}, {"ids": ["topology"]}),
            ("search", {"query": "topology"}, {"ids": ["note"]}),
            ("read", {"path": "topology.md"}, {"content": "topology evidence"}),
        ]
        decisions = [result(guard, *step)["action"] for step in workflow]
        self.assertNotIn("hard_stop", decisions)
        for page in range(1, 4):
            self.assertEqual(result(guard, "search", {"query": "x", "page": page}, {"ids": list(range(page * 10, page * 10 + 10))})["action"], "allow")
        self.assertFalse(guard.hard_stop)

    def test_exact_repeat_recovers_once_then_blocks_before_third_retry(self):
        guard = ProgressGuard()
        first = result(guard, "search", {"query": "homelab"}, {"ids": [1]})
        second = result(guard, "search", {"query": "homelab"}, {"ids": [1]})
        self.assertEqual(second["action"], "recover")
        identity = call_fingerprint("search", {"query": "homelab"})
        self.assertEqual(guard.evaluate_pre_call(identity), "hard_stop")
        self.assertEqual(guard.counters["exact_repeats"], 1)
        self.assertEqual(guard.counters["recoveries"], 1)
        self.assertTrue(guard.hard_stop)

    def test_different_queries_same_result_signal_but_do_not_stop_immediately(self):
        guard = ProgressGuard()
        result(guard, "search", {"query": "homelab"}, {"ids": [1]})
        decision = result(guard, "search", {"query": "home lab"}, {"ids": [1]})
        self.assertTrue(decision["repeated_result"])
        self.assertFalse(guard.hard_stop)

    def test_repeated_failure_and_cycle_are_recorded(self):
        guard = ProgressGuard()
        for _ in range(2):
            result(guard, "read", {"path": "missing"}, {"error": "not_found"}, "not_found")
        self.assertEqual(guard.counters["repeated_failures"], 1)
        guard = ProgressGuard()
        for name, output in (("A", 1), ("B", 2), ("A", 1), ("B", 2)):
            result(guard, name, {}, {"value": output})
        self.assertGreaterEqual(guard.counters["cycles_detected"], 1)
        guard = ProgressGuard()
        for name, output in (("A", 1), ("B", 2), ("C", 3), ("A", 1), ("B", 2), ("C", 3)):
            result(guard, name, {}, {"value": output})
        self.assertGreaterEqual(guard.counters["cycles_detected"], 1)

    def test_recovery_can_be_followed_by_material_progress_or_stagnation(self):
        guard = ProgressGuard()
        result(guard, "search", {"q": "x"}, {"ids": [1]})
        self.assertEqual(result(guard, "search", {"q": "x"}, {"ids": [1]})["action"], "recover")
        self.assertEqual(result(guard, "read", {"path": "new"}, {"content": "new evidence"})["action"], "allow")
        self.assertLess(guard.score, guard.recover_threshold)
        guard = ProgressGuard()
        result(guard, "search", {"q": "x"}, {"ids": [1]})
        self.assertEqual(result(guard, "search", {"q": "x"}, {"ids": [1]})["action"], "recover")
        self.assertEqual(guard.evaluate_pre_call(call_fingerprint("search", {"q": "x"})), "hard_stop")

    def test_approval_snapshot_restores_pending_call_and_rejection_is_not_failure(self):
        guard = ProgressGuard()
        guard.observe_call(call_fingerprint("write", {"path": "x"}))
        resumed = ProgressGuard.restore(guard.snapshot())
        resumed.observe_resumed_result({"status": "rejected"}, "user_rejected")
        self.assertEqual(resumed.counters["failed_calls"], 0)
        self.assertEqual(resumed.counters["tool_calls"], 1)

    def test_fingerprint_cost_is_local_and_state_is_bounded(self):
        started = time.perf_counter()
        for index in range(100):
            result(ProgressGuard(), "search", {"query": str(index)}, {"ids": [index]})
        self.assertLess(time.perf_counter() - started, 1)
        guard = ProgressGuard()
        for index in range(80):
            result(guard, "read", {"i": index}, {"i": index})
        self.assertEqual(len(guard.snapshot()["history"]), 64)


if __name__ == "__main__":
    unittest.main()
