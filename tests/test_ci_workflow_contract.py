from pathlib import Path
import unittest


WORKFLOW = Path(".github/workflows/quality.yml").read_text()
CHECK_JOB, PUBLISH_JOB = WORKFLOW.split("\n  publish:\n", maxsplit=1)
PUBLISH_JOB, RELEASE_JOB = PUBLISH_JOB.split("\n  release_latest_and_webhook:\n", maxsplit=1)


class CiWorkflowContractTests(unittest.TestCase):
    def test_pull_requests_do_not_publish_or_release(self):
        self.assertIn("pull_request:\n    branches: [main]", WORKFLOW)
        self.assertNotIn("secrets.", CHECK_JOB)
        self.assertNotIn("packages: write", CHECK_JOB)
        self.assertNotIn("pull_request", PUBLISH_JOB)
        self.assertNotIn("pull_request", RELEASE_JOB)

    def test_sha_publication_is_main_push_only_and_waits_for_checks(self):
        self.assertIn("needs: check", PUBLISH_JOB)
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main'", PUBLISH_JOB)
        self.assertIn("packages: write", PUBLISH_JOB)
        self.assertIn("secrets.GITHUB_TOKEN", PUBLISH_JOB)
        self.assertIn(":sha-${{ github.sha }}", PUBLISH_JOB)
        self.assertNotIn(":latest", PUBLISH_JOB)

    def test_latest_and_webhook_require_successful_migration_and_schema_gates(self):
        self.assertIn("needs: [check, publish]", RELEASE_JOB)
        self.assertIn("needs.check.outputs.migration_gate == 'no-migration-files-changed'", RELEASE_JOB)
        self.assertIn("needs.publish.outputs.deployment_eligible == 'true'", RELEASE_JOB)
        self.assertIn("PRODUCTION_SCHEMA_VERSION: ${{ vars.NEXO_PRODUCTION_SCHEMA_VERSION }}", RELEASE_JOB)
        self.assertIn('[ "$PRODUCTION_SCHEMA_VERSION" = 31 ]', RELEASE_JOB)
        self.assertIn('[ "$MIGRATION_GATE" = no-migration-files-changed ]', RELEASE_JOB)

    def test_latest_uses_the_published_digest_and_serializes_release(self):
        self.assertIn("group: nexo-main-release", RELEASE_JOB)
        self.assertIn("cancel-in-progress: false", RELEASE_JOB)
        self.assertIn('git fetch --no-tags origin main', RELEASE_JOB)
        self.assertIn('"$RELEASE_SHA" = "$(git rev-parse FETCH_HEAD)"', RELEASE_JOB)
        self.assertIn('docker buildx imagetools create --tag "$IMAGE:latest" "$IMAGE@$DIGEST"', RELEASE_JOB)
        self.assertIn("DIGEST: ${{ steps.build.outputs.digest }}", PUBLISH_JOB)

    def test_webhook_is_posted_from_secret_without_using_the_s1e_api_deployer(self):
        self.assertIn("NEXO_PORTAINER_WEBHOOK_URL: ${{ secrets.NEXO_PORTAINER_WEBHOOK_URL }}", RELEASE_JOB)
        self.assertIn("python3 scripts/portainer_webhook.py", RELEASE_JOB)
        self.assertNotIn("scripts/portainer_deploy.py", RELEASE_JOB)
        self.assertNotIn("PORTAINER_API_KEY", RELEASE_JOB)
        self.assertNotIn("/api/stacks/", RELEASE_JOB)
        self.assertNotIn("PORTAINER_WEBHOOK_URL=https://", WORKFLOW)

    def test_compose_keeps_image_variable_and_qdrant_is_not_deployed(self):
        compose = Path("docker-compose.portainer.yml").read_text()
        self.assertIn('${NEXO_IMAGE:?Set NEXO_IMAGE', compose)
        self.assertNotIn("ghcr.io/osviel91/nexo-assistant:latest", compose)
        self.assertNotIn("qdrant", RELEASE_JOB.lower())
        self.assertIn("docker-compose.qdrant.yml config --quiet", CHECK_JOB)

    def test_release_contract_keeps_schema_and_migration_eligibility(self):
        self.assertIn("is_deployment_eligible(migration_gate, schema_version)", PUBLISH_JOB)
        self.assertIn("vars.NEXO_PRODUCTION_SCHEMA_VERSION", PUBLISH_JOB)
        self.assertIn("vars.NEXO_PRODUCTION_COMMIT", CHECK_JOB)

    def test_no_services_are_started_by_the_workflow(self):
        self.assertNotIn("docker compose up", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
