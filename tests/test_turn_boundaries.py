import unittest

from app.turn_boundaries import history_for_turn, is_contextual_followup


class TurnBoundaryTests(unittest.TestCase):
    def test_failed_request_is_context_only_unless_followed_up_explicitly(self):
        history = [
            {"role": "user", "content": "Find my vault note about the router", "runtime_status": "failed"},
            {"role": "assistant", "content": "", "runtime_status": None},
        ]
        unrelated = history_for_turn(history, "What is the weather in Madrid?")
        self.assertEqual(unrelated[0]["role"], "system")
        self.assertIn("historical context only", unrelated[0]["content"])
        followup = history_for_turn(history, "Continue that search and open the note")
        self.assertEqual(followup[0]["role"], "user")

    def test_followup_markers_and_domain_change(self):
        self.assertTrue(is_contextual_followup("¿Y para pasado mañana?", "Madrid weather tomorrow"))
        self.assertFalse(is_contextual_followup("Cambiando de tema: ¿qué hora es?", "search my vault"))
        self.assertFalse(is_contextual_followup("What is the weather?", "search my vault"))


if __name__ == "__main__":
    unittest.main()
