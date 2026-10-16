from pathlib import Path
import unittest


WORKFLOW = Path(".github/workflows/quality.yml").read_text()
CHECK_JOB, PUBLISH_JOB = WORKFLOW.split("\n  publish:\n", maxsplit=1)


class CiWorkflowContractTests(unittest.TestCase):
    def test_pull_requests_only_run_read_only_checks(self):
        self.assertIn("pull_request:\n    branches: [main]", WORKFLOW)
        self.assertNotIn("secrets.", CHECK_JOB)
        self.assertNotIn("packages: write", CHECK_JOB)

    def test_publish_is_main_push_only_and_waits_for_checks(self):
        self.assertIn("needs: check", PUBLISH_JOB)
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main'", PUBLISH_JOB)
        self.assertIn("packages: write", PUBLISH_JOB)
        self.assertIn("secrets.GITHUB_TOKEN", PUBLISH_JOB)

    def test_no_product_deploy_or_webhook_is_configured(self):
        self.assertNotIn("\n  deploy:\n", WORKFLOW)
        self.assertNotIn("PORTAINER_WEBHOOK_URL", WORKFLOW)
        self.assertNotIn("curl ", WORKFLOW)

    def test_release_contract_uses_commit_tag_and_digest(self):
        self.assertIn(":sha-${{ github.sha }}", PUBLISH_JOB)
        self.assertIn("DIGEST: ${{ steps.build.outputs.digest }}", PUBLISH_JOB)
        self.assertIn('"deployment_eligible": False', PUBLISH_JOB)

    def test_split_stack_manifests_are_validated_without_running_services(self):
        self.assertIn("docker-compose.portainer.yml config --quiet", CHECK_JOB)
        self.assertIn("docker-compose.qdrant.yml config --quiet", CHECK_JOB)
        self.assertNotIn("qdrant", PUBLISH_JOB)
        self.assertNotIn("docker compose up", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
