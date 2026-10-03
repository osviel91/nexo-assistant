import tempfile
import unittest
import uuid
from pathlib import Path

from app import approvals, main


class ApprovalTests(unittest.TestCase):
    def test_arguments_are_canonical_and_summary_omits_private_values(self):
        args = {"name": "database", "password": "never-show", "api_key": "secret", "resource": "prod"}
        encoded, digest = approvals.freeze_arguments(args)
        self.assertEqual(encoded, '{"api_key":"secret","name":"database","password":"never-show","resource":"prod"}')
        self.assertEqual(len(digest), 64)
        self.assertEqual(approvals.safe_summary("service.update", "mutating", args),
                         {"tool_id": "service.update", "action": "mutating", "details": {"name": "database", "resource": "prod"}})

    def test_resolution_is_compare_and_set_and_delete_cascades(self):
        with tempfile.TemporaryDirectory() as directory:
            original = main.DB_PATH
            main.DB_PATH = Path(directory) / "approvals.sqlite3"
            try:
                main.startup()
                conversation_id, run_id, message_id = (str(uuid.uuid4()) for _ in range(3))
                with main.db() as connection:
                    connection.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)", (conversation_id, "Test", main.now(), main.now()))
                    connection.execute("INSERT INTO messages(id,conversation_id,role,content,provider_id,model_id,attachments,created_at) VALUES(?,?,?,?,?,?,?,?)", (message_id, conversation_id, "user", "run", "p", "m", "[]", main.now()))
                    connection.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status,metadata) VALUES(?,?,?,?,?,?)", (run_id, conversation_id, message_id, main.now(), "waiting_approval", "{}"))
                    approval_id = approvals.create(connection, run_id=run_id, conversation_id=conversation_id, message_id=message_id,
                        tool_call_id="call-1", tool_id="service.update", action="mutating", arguments={"name": "database", "token": "secret"}, continuation={"tool_call_count": 1}, fingerprint="a" * 64)
                with main.db() as connection:
                    result = approvals.resolve(connection, approval_id, "approve")
                self.assertTrue(result["won"])
                with main.db() as connection:
                    duplicate = approvals.resolve(connection, approval_id, "reject")
                self.assertFalse(duplicate["won"])
                self.assertEqual(duplicate["status"], "approved")
                with main.db() as connection:
                    connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM pending_approvals WHERE id=?", (approval_id,)).fetchone()[0], 0)
            finally:
                main.DB_PATH = original


if __name__ == "__main__":
    unittest.main()
