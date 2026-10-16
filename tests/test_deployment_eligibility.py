import unittest

from scripts.deployment_eligibility import classify_migration_changes


class DeploymentEligibilityTests(unittest.TestCase):
    def test_unknown_base_fails_closed(self):
        self.assertEqual(classify_migration_changes([], base_known=False), "unknown")

    def test_migration_sources_require_compatibility_review(self):
        for path in ("app/migrations.py", "app/safe_migrations.py", "app/db/migrations/new.py"):
            with self.subTest(path=path):
                self.assertEqual(
                    classify_migration_changes([path], base_known=True), "review-required"
                )

    def test_unrelated_change_does_not_claim_migration_compatibility(self):
        self.assertEqual(
            classify_migration_changes(["app/main.py", "web/index.html"], base_known=True),
            "no-migration-files-changed",
        )


if __name__ == "__main__":
    unittest.main()
