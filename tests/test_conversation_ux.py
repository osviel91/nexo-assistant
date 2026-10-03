import json
import tempfile
import unittest
from pathlib import Path


class ConversationUXTests(unittest.TestCase):
    def setUp(self):
        from app import main

        self.main = main
        self.directory = tempfile.TemporaryDirectory()
        self.old_db = main.DB_PATH
        main.DB_PATH = Path(self.directory.name) / "ux.sqlite3"
        main.startup()

    def tearDown(self):
        self.main.DB_PATH = self.old_db
        self.directory.cleanup()

    def test_preferences_are_independent_and_metrics_are_normalized(self):
        main = self.main
        main.update_preferences(main.PreferencesPatch(last_chat_model="p::chat", last_agent_profile="agent-1"))
        self.assertEqual(main.get_preferences(), {"last_chat_model": "p::chat", "last_agent_profile": "agent-1"})
        self.assertEqual(main.normalized_metrics({"prompt_tokens": 10, "completion_tokens": 5, "context_window": 100}), {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": None, "context_window": 100,
            "context_utilization": 0.1, "ttft_ms": None, "provider_ttft_ms": None,
            "request_to_first_token_ms": None, "generation_ms": None, "total_request_ms": None,
            "thinking_duration_ms": None,
            "thinking_tokens": None, "generation_duration_ms": None, "total_duration_ms": None,
            "tokens_per_second": None,
        })

    def test_branch_copies_history_and_configuration_without_reusing_message_ids(self):
        main = self.main
        conversation_id, message_id = "conversation", "message"
        with main.db() as connection:
            connection.execute("INSERT INTO conversations(id,title,created_at,updated_at,execution_mode,agent_profile_id,notebook_id) VALUES(?,?,?,?,?,?,?)", (conversation_id, "Original", "1", "2", "agent", "agent-1", "notebook-1"))
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,provider_id,model_id,attachments,sources,runtime_metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (message_id, conversation_id, "assistant", "answer", "provider", "model", "[]", "[]", json.dumps({"metrics": {"output_tokens": 4}}), "2"))
        branch = main.branch_conversation(conversation_id, message_id)
        data = main.get_conversation(branch["id"])
        self.assertEqual(data["conversation"]["notebook_id"], "notebook-1")
        self.assertEqual(data["messages"][0]["content"], "answer")
        self.assertNotEqual(data["messages"][0]["id"], message_id)

    def test_tools_toggle_persists_per_conversation_and_is_copied_to_branches(self):
        main = self.main
        conversation_id, message_id = "tools-chat", "tools-message"
        with main.db() as connection:
            connection.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)", (conversation_id, "Tools", "1", "2"))
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,created_at) VALUES(?,?,?,?,?)", (message_id, conversation_id, "user", "hello", "2"))

        self.assertTrue(main.get_conversation(conversation_id)["conversation"]["tools_enabled"])
        main.update_conversation(conversation_id, main.ConversationPatch(tools_enabled=False))
        self.assertFalse(main.get_conversation(conversation_id)["conversation"]["tools_enabled"])

        branch = main.branch_conversation(conversation_id, message_id)
        self.assertFalse(main.get_conversation(branch["id"])["conversation"]["tools_enabled"])

    def test_thinking_contract_does_not_expose_hidden_content(self):
        main = self.main
        result = main.public_runtime({"thinking_available": True, "thinking_content": "private", "thinking_tokens": 7})
        self.assertIsNone(result["thinking"]["content"])
        self.assertNotIn("thinking_content", result)

    def test_reasoning_content_capability_exposes_streamed_thinking_panel(self):
        result = self.main.public_runtime({"thinking_content_available": True, "thinking_content": "step", "thinking_available": False})
        self.assertTrue(result["thinking"]["available"])
        self.assertEqual(result["thinking"]["content"], "step")


if __name__ == "__main__":
    unittest.main()
