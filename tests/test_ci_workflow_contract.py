from pathlib import Path
import unittest


WORKFLOW = Path(".github/workflows/quality.yml").read_text()
CHECK_JOB, PUBLISH_JOB = WORKFLOW.split("\n  publish:\n", maxsplit=1)
PUBLISH_JOB, DEPLOY_JOB = PUBLISH_JOB.split("\n  deploy:\n", maxsplit=1)


class CiWorkflowContractTests(unittest.TestCase):
    def test_pull_requests_only_run_read_only_checks(self):
        self.assertIn("pull_request:\n    branches: [main]", WORKFLOW)
        self.assertNotIn("secrets.", CHECK_JOB)
        self.assertNotIn("packages: write", CHECK_JOB)

    def test_publish_is_main_push_only_and_waits_for_checks(self):
        self.assertIn("needs: check", PUBLISH_JOB)
        self.assertIn("github.event_name == 'push' || github.event_name == 'workflow_dispatch'", PUBLISH_JOB)
        self.assertIn("github.ref == 'refs/heads/main'", PUBLISH_JOB)
        self.assertIn("packages: write", PUBLISH_JOB)
        self.assertIn("secrets.GITHUB_TOKEN", PUBLISH_JOB)

    def test_deploy_is_hard_disabled_and_cannot_run_on_push_or_merge(self):
        self.assertIn("workflow_dispatch:", WORKFLOW)
        self.assertIn("default: false", WORKFLOW)
        self.assertIn("if: ${{ false && github.event_name == 'workflow_dispatch'", DEPLOY_JOB)
        self.assertIn("inputs.confirm_production_deploy", DEPLOY_JOB)
        self.assertNotIn("event_name == 'push'", DEPLOY_JOB)

    def test_deploy_requires_eligibility_and_serializes_production_updates(self):
        self.assertIn("needs: [check, publish]", DEPLOY_JOB)
        self.assertIn("needs.publish.outputs.deployment_eligible == 'true'", DEPLOY_JOB)
        self.assertIn("needs.publish.outputs.migration_gate == 'no-migration-files-changed'", DEPLOY_JOB)
        self.assertIn("group: nexo-portainer-production", DEPLOY_JOB)
        self.assertIn("cancel-in-progress: false", DEPLOY_JOB)
        self.assertIn('git rev-parse FETCH_HEAD', DEPLOY_JOB)
        self.assertIn('"$DEPLOY_SHA" = "$(git rev-parse FETCH_HEAD)"', DEPLOY_JOB)

    def test_deployer_uses_environment_secrets_and_the_published_digest(self):
        self.assertIn("environment: nexo-production", DEPLOY_JOB)
        self.assertIn("secrets.PORTAINER_URL", DEPLOY_JOB)
        self.assertIn("secrets.PORTAINER_API_KEY", DEPLOY_JOB)
        self.assertIn("vars.NEXO_HEALTHCHECK_URL", DEPLOY_JOB)
        self.assertIn("PORTAINER_STACK_ID: '30'", DEPLOY_JOB)
        self.assertIn("${{ needs.publish.outputs.image }}@${{ needs.publish.outputs.digest }}", DEPLOY_JOB)
        self.assertIn("python3 scripts/portainer_deploy.py", DEPLOY_JOB)

    def test_no_webhook_or_qdrant_deployment_is_configured(self):
        self.assertNotIn("PORTAINER_WEBHOOK_URL", WORKFLOW)
        self.assertNotIn("qdrant", DEPLOY_JOB.lower())

    def test_release_contract_uses_commit_tag_and_digest(self):
        self.assertIn(":sha-${{ github.sha }}", PUBLISH_JOB)
        self.assertIn("DIGEST: ${{ steps.build.outputs.digest }}", PUBLISH_JOB)
        self.assertIn("is_deployment_eligible(migration_gate, schema_version)", PUBLISH_JOB)
        self.assertIn('"deployment_lock": "hard-disabled"', PUBLISH_JOB)
        self.assertIn("vars.NEXO_PRODUCTION_SCHEMA_VERSION", PUBLISH_JOB)
        self.assertIn("vars.NEXO_PRODUCTION_COMMIT", CHECK_JOB)

    def test_split_stack_manifests_are_validated_without_running_services(self):
        self.assertIn("docker-compose.portainer.yml config --quiet", CHECK_JOB)
        self.assertIn("docker-compose.qdrant.yml config --quiet", CHECK_JOB)
        self.assertNotIn("qdrant", PUBLISH_JOB)
        self.assertNotIn("docker compose up", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
