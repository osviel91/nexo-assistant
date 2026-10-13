import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import admin_cli, migrations, safe_migrations


def make_fixture(path: Path, version: int = 31, *, admin_state: str = "schema_installed") -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for number, migration in migrations.MIGRATIONS:
            if number > 28:
                break
            migration(connection)
            connection.execute("INSERT INTO schema_migrations VALUES(?, 'synthetic')", (number,))
        for spec in safe_migrations.TRANSACTIONAL_MIGRATIONS:
            if spec.version > version:
                break
            with safe_migrations.DatabaseLease(path, exclusive=True) as lease:
                safe_migrations._apply_transactional_migration(
                    connection, lease, spec.version, spec.statements,
                    spec.validate_previous, spec.validate_output,
                )
        if version >= 29 and admin_state == "bootstrap_completed":
            connection.execute("INSERT INTO auth_users VALUES('fixture-admin','synthetic','fixture-admin','not-a-real-hash','active',1,1)")
            connection.execute("INSERT INTO auth_user_permissions VALUES('fixture-admin','config:read')")
            connection.execute("INSERT INTO auth_security_events(event_type,actor_user_id,created_at) VALUES('bootstrap_completed','fixture-admin',1)")
            connection.execute("UPDATE auth_state SET state='bootstrap_completed',bootstrapped_at='synthetic' WHERE id=1")
        connection.execute(f"PRAGMA application_id={admin_cli.SYNTHETIC_APPLICATION_ID}")
    finally:
        connection.close()
    os.chmod(path, 0o600)


def make_backup(source: Path, backup: Path) -> None:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    os.chmod(backup, 0o600)


class AdminCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "fixture.sqlite3"
        make_fixture(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = admin_cli.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_supported_schema_versions_28_through_31(self):
        for version in (28, 29, 30, 31):
            with self.subTest(version=version):
                database = self.root / f"v{version}.sqlite3"
                make_fixture(database, version)
                code, output, _ = self.invoke("inspect", "--database", str(database))
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["schema_version"], version)

    def test_status_distinguishes_uninstalled_incomplete_and_completed(self):
        for version, state in ((28, "not_installed"), (31, "schema_installed"),
                               (31, "bootstrap_completed")):
            database = self.root / f"status-{version}-{state}.sqlite3"
            make_fixture(database, version, admin_state=state)
            code, output, _ = self.invoke("status", "--database", str(database))
            self.assertEqual(code, 0)
            result = json.loads(output)
            self.assertEqual(result["administrative_state"], state)
            self.assertFalse(result["operational_readiness"])
            self.assertIn("operational_readiness_not_assessed", result["blockers"])

    def test_ledger_gap_unknown_schema_and_corruption_fail_closed(self):
        gap = self.root / "gap.sqlite3"
        make_fixture(gap, 29)
        with sqlite3.connect(gap) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=15")
        unknown = self.root / "unknown.sqlite3"
        with sqlite3.connect(unknown) as connection:
            connection.execute(f"PRAGMA application_id={admin_cli.SYNTHETIC_APPLICATION_ID}")
            connection.execute("CREATE TABLE surprise(value TEXT)")
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not sqlite")
        for database in (gap, unknown, corrupt):
            with self.subTest(database=database.name):
                code, _, stderr = self.invoke("inspect", "--database", str(database))
                self.assertEqual(code, 4)
                self.assertNotIn("not sqlite", stderr)

    def test_unexpected_generation_is_rejected(self):
        code, output, _ = self.invoke("inspect", "--database", str(self.database),
                                      "--expected-generation", "not-the-generation")
        self.assertEqual(code, 4)
        self.assertNotIn("not-the-generation", output)

    def test_path_rejects_symlink_product_path_and_insecure_permissions(self):
        link = self.root / "linked.sqlite3"
        link.symlink_to(self.database)
        outside = Path(self.temp.name).parent / "nexo.sqlite3"
        missing = self.root / "missing.sqlite3"
        for database in (link, outside, missing):
            with self.subTest(database=database):
                code, _, _ = self.invoke("check-path", "--database", str(database))
                self.assertEqual(code, 3 if database == missing else 2)
        os.chmod(self.database, 0o666)
        code, _, _ = self.invoke("check-path", "--database", str(self.database))
        self.assertEqual(code, 2)
        os.chmod(self.database, 0o000)
        code, _, _ = self.invoke("check-path", "--database", str(self.database))
        self.assertEqual(code, 3)
        os.chmod(self.database, 0o600)

    def test_backup_verify_checks_integrity_schema_and_target_generation(self):
        good = self.root / "backup.sqlite3"
        make_backup(self.database, good)
        code, output, _ = self.invoke("backup-verify", "--database", str(self.database),
                                      "--backup", str(good))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["restore_tested"])

        other = self.root / "other.sqlite3"
        make_fixture(other, 28)
        code, _, _ = self.invoke("backup-verify", "--database", str(self.database),
                                 "--backup", str(other))
        self.assertEqual(code, 4)
        corrupt = self.root / "backup-corrupt.sqlite3"
        corrupt.write_bytes(b"damaged")
        code, _, _ = self.invoke("backup-verify", "--database", str(self.database),
                                 "--backup", str(corrupt))
        self.assertEqual(code, 4)

    def test_read_commands_leave_database_and_directory_unchanged(self):
        backup = self.root / "backup.sqlite3"
        make_backup(self.database, backup)
        before = self.database.read_bytes()
        backup_before = backup.read_bytes()
        entries = sorted(path.name for path in self.root.iterdir())
        for command in ("inspect", "status", "check-path"):
            code, _, _ = self.invoke(command, "--database", str(self.database))
            self.assertEqual(code, 0)
        code, _, _ = self.invoke("backup-verify", "--database", str(self.database),
                                 "--backup", str(backup))
        self.assertEqual(code, 0)
        after_entries = sorted(path.name for path in self.root.iterdir())
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(backup.read_bytes(), backup_before)
        self.assertEqual(after_entries, entries)
        self.assertEqual(admin_cli._sidecars(self.database), ())

    def test_wal_journal_mode_without_sidecars_fails_without_creating_them(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        self.assertEqual(admin_cli._sidecars(self.database), ())
        before = self.database.read_bytes()
        code, _, _ = self.invoke("inspect", "--database", str(self.database))
        self.assertEqual(code, 4)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(admin_cli._sidecars(self.database), ())

    def test_wal_sidecars_are_rejected_without_cleanup_or_mutation(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO providers(id,name,base_url,api_key,created_at) VALUES('p','synthetic','https://invalid','secret-fixture','now')")
            connection.commit()
            sidecars = set(path.name for path in self.root.iterdir())
            self.assertTrue(any(name.endswith("-wal") for name in sidecars))
            code, _, _ = self.invoke("inspect", "--database", str(self.database))
            self.assertEqual(code, 4)
            self.assertEqual(set(path.name for path in self.root.iterdir()), sidecars)
        finally:
            connection.close()

    def test_output_is_redacted_and_repeated_calls_are_stable(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO providers(id,name,base_url,api_key,created_at) VALUES('p','synthetic','https://invalid','NEVER-PRINT-THIS','now')")
        first = self.invoke("inspect", "--database", str(self.database))
        second = self.invoke("inspect", "--database", str(self.database))
        self.assertEqual(first, second)
        self.assertNotIn("NEVER-PRINT-THIS", first[1] + first[2])

    def test_cli_module_does_not_import_http_runtime(self):
        script = "import sys; import app.admin_cli; assert 'app.main' not in sys.modules"
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_race_is_not_claimed_as_prevented(self):
        code, output, _ = self.invoke("check-path", "--database", str(self.database))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["race_free_open_guarantee"])


if __name__ == "__main__":
    unittest.main()
