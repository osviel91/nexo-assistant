import os
import json
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
import uuid
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from app import main, migrations
from prototypes.admin_persistence import backup_database, restore_database
from app.safe_migrations import (
    DatabaseInUse,
    DatabaseLease,
    HISTORICAL_SCHEMA_VERSION,
    MigrationRecoveryRequired,
    SafeMigrationEngine,
    SchemaCompatibilityError,
    TRANSACTIONAL_MIGRATIONS,
    _apply_transactional_migration,
    acquire_runtime_lease,
    capture_schema_manifest,
    inspect_database,
    validate_schema_manifest,
    validate_runtime_database,
)


@contextmanager
def database(path: Path, *, isolation_level="DEFERRED"):
    connection = sqlite3.connect(path, isolation_level=isolation_level)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def create_historical_database(path: Path, through: int, *, preferences: bool = False) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        if through:
            connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            for version, migration in migrations.MIGRATIONS:
                if version > through:
                    break
                migration(connection)
                connection.execute("INSERT INTO schema_migrations VALUES(?, 'fixture')", (version,))
            connection.execute("INSERT INTO providers VALUES('fixture-provider','synthetic','https://invalid/','','fixture')")
        if preferences:
            connection.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL)")
            connection.execute("INSERT INTO user_preferences VALUES('last_chat_model','synthetic','fixture')")
    finally:
        connection.close()


def wait_ready(process: subprocess.Popen[str]) -> None:
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        if not selector.select(20):
            raise AssertionError("migration child did not reach fault barrier")
        if process.stdout.readline().strip() != "READY":
            raise AssertionError("migration child exited before fault barrier")
    finally:
        selector.close()


def restore_fixture(backup: Path, destination: Path) -> None:
    staging = destination.with_name("restore-candidate.sqlite3")
    restore_database(backup, staging)
    os.replace(staging, destination)


class SafeMigrationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_offline_engine_stages_historical_versions_and_preserves_backup(self):
        for version in (1, 12, 21, 27):
            with self.subTest(version=version):
                data_dir = self.root / f"upgrade-v{version}"
                data_dir.mkdir()
                source = data_dir / "nexo.sqlite3"
                create_historical_database(source, version, preferences=version >= 21)
                before = inspect_database(source)
                result = SafeMigrationEngine(data_dir).migrate()

                self.assertEqual((result.source_version, result.target_version, result.changed),
                                 (version, 29, True))
                self.assertIsNotNone(result.backup_path)
                self.assertEqual(inspect_database(result.backup_path).version, version)
                self.assertEqual(validate_runtime_database(source).version, 29)
                with database(source) as connection:
                    self.assertEqual(connection.execute(
                        "SELECT name FROM providers WHERE id='fixture-provider'").fetchone()[0], "synthetic")
                    if version >= 21:
                        self.assertEqual(connection.execute(
                            "SELECT value FROM user_preferences WHERE key='last_chat_model'").fetchone()[0],
                            "synthetic")
                self.assertEqual(before.version, version)
                self.assertFalse(Path(str(source) + "-wal").exists())
                self.assertFalse(Path(str(source) + "-shm").exists())

    def test_offline_engine_initializes_fresh_database_and_noops_on_valid_v29(self):
        data_dir = self.root / "fresh"
        data_dir.mkdir()
        engine = SafeMigrationEngine(data_dir)
        first = engine.migrate()
        source = data_dir / "nexo.sqlite3"
        before = source.read_bytes()
        second = engine.migrate()
        self.assertEqual((first.source_version, first.target_version, first.changed), (0, 29, True))
        self.assertEqual((second.source_version, second.target_version, second.changed), (29, 29, False))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(validate_runtime_database(source).version, 29)

    def test_failure_after_staging_leaves_original_bytes_unchanged(self):
        data_dir = self.root / "staging-failure"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 27)
        original = source.read_bytes()

        def fail(stage):
            if stage == "after_staging":
                raise RuntimeError("synthetic injected failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            SafeMigrationEngine(data_dir).migrate(fail)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(inspect_database(source).version, 27)
        self.assertFalse((data_dir / ".nexo.sqlite3.promotion.json").exists())
        self.assertEqual(list(data_dir.glob(".nexo.sqlite3.stage.*.sqlite3")), [])

    def test_source_change_after_backup_is_detected_and_not_overwritten(self):
        data_dir = self.root / "changed-source"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 27)

        def concurrent_writer(stage):
            if stage == "after_staging":
                with sqlite3.connect(source) as connection:
                    connection.execute("INSERT INTO providers VALUES('external','synthetic','https://invalid/','','fixture')")

        with self.assertRaises(DatabaseInUse):
            SafeMigrationEngine(data_dir).migrate(concurrent_writer)
        with sqlite3.connect(source) as connection:
            self.assertEqual(connection.execute("SELECT name FROM providers WHERE id='external'").fetchone()[0],
                             "synthetic")
            self.assertEqual(connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 27)
        self.assertFalse((data_dir / ".nexo.sqlite3.promotion.json").exists())

    def test_wal_source_is_rejected_without_modifying_database_or_sidecars(self):
        data_dir = self.root / "wal-source"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 27)
        connection = sqlite3.connect(source, isolation_level=None)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO providers VALUES('wal-row','synthetic','https://invalid/','','fixture')")
            self.assertTrue(Path(str(source) + "-wal").exists())
            main_bytes = source.read_bytes()
            wal_bytes = Path(str(source) + "-wal").read_bytes()
            with self.assertRaises(DatabaseInUse):
                SafeMigrationEngine(data_dir).migrate()
            self.assertEqual(source.read_bytes(), main_bytes)
            self.assertEqual(Path(str(source) + "-wal").read_bytes(), wal_bytes)
        finally:
            connection.close()

    def test_active_runtime_lease_blocks_offline_engine_before_database_access(self):
        data_dir = self.root / "runtime-lease"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 27)
        original = source.read_bytes()
        lease = acquire_runtime_lease(source)
        try:
            with self.assertRaises(DatabaseInUse):
                SafeMigrationEngine(data_dir).migrate()
            self.assertEqual(source.read_bytes(), original)
        finally:
            lease.close()

    @unittest.skipUnless(os.name == "posix", "flock process coordination requires POSIX")
    def test_independent_worker_and_promoter_exclude_each_other_and_sigkill_releases_lock(self):
        data_dir = self.root / "process-locks"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 27)
        worker_script = """
import sys
from app.safe_migrations import acquire_runtime_lease
lease = acquire_runtime_lease(sys.argv[1])
print('READY', flush=True)
sys.stdin.read(1)
"""
        worker = subprocess.Popen([sys.executable, "-c", worker_script, str(source)],
                                  cwd=Path(__file__).parents[1], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        try:
            wait_ready(worker)
            with self.assertRaises(DatabaseInUse):
                SafeMigrationEngine(data_dir).migrate()
            worker.send_signal(signal.SIGKILL)
            self.assertEqual(worker.wait(timeout=10), -signal.SIGKILL)
            worker.communicate(timeout=10)
            self.assertEqual(SafeMigrationEngine(data_dir).migrate().target_version, 29)
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=10)
            worker.communicate(timeout=10)

        promoter_script = """
import sys
from app.safe_migrations import SafeMigrationEngine
def fault(stage):
    if stage == 'before_replace':
        print('READY', flush=True)
        sys.stdin.read(1)
SafeMigrationEngine(sys.argv[1]).migrate(fault)
"""
        promoter_dir = self.root / "process-promoter"
        promoter_dir.mkdir()
        promoter_source = promoter_dir / "nexo.sqlite3"
        create_historical_database(promoter_source, 27)
        promoter = subprocess.Popen([sys.executable, "-c", promoter_script, str(promoter_dir)],
                                    cwd=Path(__file__).parents[1], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        try:
            wait_ready(promoter)
            with self.assertRaises(DatabaseInUse):
                acquire_runtime_lease(promoter_source)
            promoter.send_signal(signal.SIGKILL)
            self.assertEqual(promoter.wait(timeout=10), -signal.SIGKILL)
            promoter.communicate(timeout=10)
            lease = acquire_runtime_lease(promoter_source)
            lease.close()
        finally:
            if promoter.poll() is None:
                promoter.kill()
                promoter.wait(timeout=10)
            promoter.communicate(timeout=10)
        self.assertEqual(SafeMigrationEngine(promoter_dir).recover(), "promotion-not-started")

    def test_database_helper_cannot_bypass_runtime_startup_lease(self):
        data_dir = self.root / "unguarded-db-helper"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 28)
        old_db = main.DB_PATH
        main.DB_PATH = source
        try:
            with self.assertRaisesRegex(RuntimeError, "runtime lease"):
                main.db()
            main.startup()
            with main.db() as connection:
                self.assertGreater(connection.execute("PRAGMA schema_version").fetchone()[0], 0)
        finally:
            main.shutdown()
            main.DB_PATH = old_db

    def test_active_sqlite_reader_blocks_promotion_without_changing_original(self):
        data_dir = self.root / "active-reader"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 27)
        original = source.read_bytes()
        reader = sqlite3.connect(source, isolation_level=None, timeout=0.2)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM providers").fetchone()
        try:
            with self.assertRaises(DatabaseInUse):
                SafeMigrationEngine(data_dir).migrate()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(inspect_database(source).version, 27)
        finally:
            reader.rollback()
            reader.close()

    def test_startup_is_read_only_fail_closed_for_unsupported_schema_then_accepts_promoted_target(self):
        data_dir = self.root / "runtime-startup"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 27)
        original = source.read_bytes()
        old_db = main.DB_PATH
        main.DB_PATH = source
        try:
            with self.assertRaises(SchemaCompatibilityError):
                main.startup()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(inspect_database(source).version, 27)
            SafeMigrationEngine(data_dir).migrate()
            before_startup = source.read_bytes()
            main.startup()
            self.assertEqual(source.read_bytes(), before_startup)
            with self.assertRaises(DatabaseInUse):
                SafeMigrationEngine(data_dir).migrate()
            lease = main.runtime_database_lease
            main.shutdown()
            with self.assertRaises(DatabaseInUse):
                SafeMigrationEngine(data_dir).migrate()
            lease.close()
            main.runtime_database_lease = None
        finally:
            main.shutdown()
            main.DB_PATH = old_db

    def test_startup_fails_closed_when_sqlite_storage_is_missing(self):
        data_dir = self.root / "missing-storage"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        old_db = main.DB_PATH
        main.DB_PATH = source
        try:
            with self.assertRaises(SchemaCompatibilityError):
                main.startup()
            self.assertFalse(source.exists())
        finally:
            main.shutdown()
            main.DB_PATH = old_db

    def test_startup_and_http_settings_reads_do_not_migrate_persistent_database(self):
        data_dir = self.root / "no-http-migrations"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 28)
        before_startup = source.read_bytes()
        old_db = main.DB_PATH
        main.DB_PATH = source
        try:
            with patch.object(migrations, "migrate", side_effect=AssertionError("runtime called migrate")):
                with TestClient(main.app) as client:
                    response = client.get("/api/settings/embeddings")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(validate_runtime_database(source).version, 28)
            self.assertEqual(source.read_bytes(), before_startup)
        finally:
            main.shutdown()
            main.DB_PATH = old_db

    def test_restored_incompatible_backup_is_rejected_by_real_runtime_startup(self):
        data_dir = self.root / "restored-incompatible"
        data_dir.mkdir()
        source = data_dir / "v27-source.sqlite3"
        backup = data_dir / "v27-backup.sqlite3"
        restored = data_dir / "nexo.sqlite3"
        create_historical_database(source, 27)
        backup_database(source, backup)
        restore_fixture(backup, restored)
        original = restored.read_bytes()
        old_db = main.DB_PATH
        main.DB_PATH = restored
        try:
            with self.assertRaises(SchemaCompatibilityError):
                main.startup()
            self.assertEqual(restored.read_bytes(), original)
            self.assertEqual(inspect_database(restored).version, 27)
        finally:
            main.shutdown()
            main.DB_PATH = old_db

    @unittest.skipUnless(os.name == "posix", "promotion SIGKILL synchronization requires POSIX")
    def test_next_process_recovers_and_validates_active_generation_after_sigkill(self):
        for stage, expected in (("before_replace", "27 29 True"),
                                ("after_replace", "29 29 False"),
                                ("after_validation", "29 29 False")):
            with self.subTest(stage=stage):
                data_dir = self.root / stage
                data_dir.mkdir()
                source = data_dir / "nexo.sqlite3"
                create_historical_database(source, 27)
                child = """
import sys
from app.safe_migrations import SafeMigrationEngine
def fault(stage):
    if stage == sys.argv[2]:
        print('READY', flush=True)
        sys.stdin.read(1)
SafeMigrationEngine(sys.argv[1]).migrate(fault)
"""
                process = subprocess.Popen([sys.executable, "-c", child, str(data_dir), stage],
                                           cwd=Path(__file__).parents[1], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                try:
                    wait_ready(process)
                    process.send_signal(signal.SIGKILL)
                    self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                    process.communicate(timeout=10)

                journal = data_dir / ".nexo.sqlite3.promotion.json"
                self.assertTrue(journal.exists())
                with self.assertRaises(MigrationRecoveryRequired):
                    validate_runtime_database(source)
                recovery_process = subprocess.run(
                    [sys.executable, "-c", "import sys; from app.safe_migrations import SafeMigrationEngine; r=SafeMigrationEngine(sys.argv[1]).migrate(); print(r.source_version, r.target_version, r.changed)", str(data_dir)],
                    cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=30)
                self.assertEqual(recovery_process.returncode, 0, recovery_process.stderr)
                self.assertEqual(recovery_process.stdout.strip(), expected)
                self.assertFalse(journal.exists())
                self.assertEqual(validate_runtime_database(source).version, 29)
                with database(source) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(tuple(row[0] for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version")), tuple(range(1, 30)))
                    self.assertEqual(connection.execute("SELECT name FROM providers "
                                                        "WHERE id='fixture-provider'").fetchone()[0], "synthetic")
                    self.assertEqual(connection.execute("SELECT state FROM auth_state WHERE id=1").fetchone()[0],
                                     "schema_installed")
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_user_permissions").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)

    @unittest.skipUnless(os.name == "posix", "recovery process tests require POSIX")
    def test_recovery_rejects_corrupt_backup_and_ambiguous_active_generation(self):
        for tamper in ("backup", "database", "journal"):
            with self.subTest(tamper=tamper):
                data_dir = self.root / f"recovery-{tamper}"
                data_dir.mkdir()
                source = data_dir / "nexo.sqlite3"
                create_historical_database(source, 27)
                child = """
import sys
from app.safe_migrations import SafeMigrationEngine
def fault(stage):
    if stage == 'after_replace':
        print('READY', flush=True)
        sys.stdin.read(1)
SafeMigrationEngine(sys.argv[1]).migrate(fault)
"""
                process = subprocess.Popen([sys.executable, "-c", child, str(data_dir)],
                                           cwd=Path(__file__).parents[1], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                try:
                    wait_ready(process)
                    process.send_signal(signal.SIGKILL)
                    self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                    process.communicate(timeout=10)

                journal_path = data_dir / ".nexo.sqlite3.promotion.json"
                journal = json.loads(journal_path.read_text())
                backup = data_dir / journal["backup"]
                if tamper == "backup":
                    backup.write_bytes(b"not a sqlite backup")
                elif tamper == "database":
                    with sqlite3.connect(source) as connection:
                        connection.execute("INSERT INTO providers VALUES('ambiguous','synthetic','https://invalid/','','fixture')")
                else:
                    journal["target_digest"] = journal["source_digest"]
                    journal_path.write_text(json.dumps(journal))
                with self.assertRaises(MigrationRecoveryRequired):
                    SafeMigrationEngine(data_dir).recover()
                self.assertTrue(journal_path.exists())
                self.assertTrue(backup.exists())

    def test_migration_cleans_abandoned_staging_and_validates_backup_integrity(self):
        data_dir = self.root / "orphan-staging"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 27)
        orphan = data_dir / ".nexo.sqlite3.stage.abandoned.sqlite3"
        orphan.write_bytes(b"partial uncommitted staging file")
        result = SafeMigrationEngine(data_dir).migrate()
        self.assertFalse(orphan.exists())
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(inspect_database(result.backup_path).version, 27)
        self.assertEqual(validate_runtime_database(source).version, 29)

    def test_incompatible_runtime_states_never_start_api_or_mutate_database(self):
        cases = ("old", "future", "ledger-gap", "partial", "unledgered")
        for case in cases:
            with self.subTest(case=case):
                data_dir = self.root / f"startup-{case}"
                data_dir.mkdir()
                source = data_dir / "nexo.sqlite3"
                if case == "old":
                    create_historical_database(source, 27)
                elif case == "unledgered":
                    with sqlite3.connect(source) as connection:
                        connection.execute("CREATE TABLE partial(value TEXT)")
                else:
                    create_historical_database(source, 28)
                    with sqlite3.connect(source) as connection:
                        if case == "future":
                            connection.execute("INSERT INTO schema_migrations VALUES(29, 'future')")
                        elif case == "ledger-gap":
                            connection.execute("DELETE FROM schema_migrations WHERE version=14")
                        elif case == "partial":
                            connection.execute("DROP TABLE pending_approvals")
                original = source.read_bytes()
                old_db = main.DB_PATH
                main.DB_PATH = source
                try:
                    with patch.object(migrations, "migrate", side_effect=AssertionError("startup attempted migration")):
                        with self.assertRaises(SchemaCompatibilityError):
                            with TestClient(main.app) as client:
                                client.get("/api/settings/embeddings")
                    self.assertEqual(source.read_bytes(), original)
                finally:
                    main.shutdown()
                    main.DB_PATH = old_db

    def test_preference_extension_is_supported_only_with_exact_structure(self):
        data_dir = self.root / "preferences"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 28, preferences=True)
        self.assertTrue(validate_runtime_database(source).user_preferences)
        with sqlite3.connect(source) as connection:
            connection.execute("ALTER TABLE user_preferences ADD COLUMN unexpected TEXT")
        with self.assertRaises(SchemaCompatibilityError):
            validate_runtime_database(source)

    def test_runtime_manifest_ignores_sql_format_but_rejects_literal_or_constraint_drift(self):
        canonical = self.root / "canonical-format.sqlite3"
        variant = self.root / "variant-format.sqlite3"
        changed = self.root / "changed-literal.sqlite3"
        for path, sql in (
            (canonical, "CREATE TABLE cp2b1_format(id INTEGER PRIMARY KEY, value TEXT NOT NULL DEFAULT 'Hello' CHECK(value <> 'blocked'))"),
            (variant, "create /* harmless comment */ table cp2b1_format ( id integer primary key, value text not null default 'Hello' check ( value <> 'blocked' ) )"),
            (changed, "CREATE TABLE cp2b1_format(id INTEGER PRIMARY KEY, value TEXT NOT NULL DEFAULT 'hello' CHECK(value <> 'blocked'))"),
        ):
            with sqlite3.connect(path) as connection:
                connection.execute(sql)

        with sqlite3.connect(canonical) as connection:
            manifest = capture_schema_manifest(connection)
        with sqlite3.connect(variant) as connection:
            validate_schema_manifest(connection, manifest)
        with sqlite3.connect(changed) as connection:
            with self.assertRaises(SchemaCompatibilityError):
                validate_schema_manifest(connection, manifest)

    def test_new_migration_contract_is_lease_only_and_registered(self):
        self.assertEqual(tuple(item.version for item in TRANSACTIONAL_MIGRATIONS), (29,))
        data_dir = self.root / "synthetic-future"
        data_dir.mkdir()
        source = data_dir / "base.sqlite3"
        expected = data_dir / "expected.sqlite3"
        failed = data_dir / "failed.sqlite3"
        create_historical_database(source, 28)
        create_historical_database(failed, 28)
        with database(source) as src, database(expected) as dst:
            src.backup(dst)
            dst.execute("CREATE TABLE cp2b1_synthetic(id INTEGER PRIMARY KEY, detail TEXT NOT NULL)")
            dst.execute("INSERT INTO cp2b1_synthetic VALUES(1, 'synthetic')")

        with database(source) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            previous = capture_schema_manifest(connection)
            with self.assertRaises(DatabaseInUse):
                _apply_transactional_migration(
                    connection, None, 29, (), lambda _db: None, lambda _db: None)
        with database(source) as connection, database(expected) as expected_db:
            connection.execute("PRAGMA foreign_keys=ON")
            output = capture_schema_manifest(expected_db)
            statements = (
                "CREATE TABLE cp2b1_synthetic(id INTEGER PRIMARY KEY, detail TEXT NOT NULL)",
                "INSERT INTO cp2b1_synthetic VALUES(1, 'synthetic')",
            )

            def validate_before(db):
                validate_schema_manifest(db, previous)

            def validate_after(db):
                validate_schema_manifest(db, output)

            with DatabaseLease(source, exclusive=True) as lease:
                self.assertTrue(_apply_transactional_migration(
                    connection, lease, 29, statements, validate_before, validate_after))
                self.assertEqual(tuple(row[0] for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version")), tuple(range(1, 30)))
                self.assertFalse(_apply_transactional_migration(
                    connection, lease, 29, statements, validate_before, validate_after))

        with database(failed) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with DatabaseLease(failed, exclusive=True) as lease:
                def fail_after_ledger(stage):
                    if stage == "after_ledger":
                        raise sqlite3.OperationalError("fixture fault")

                with self.assertRaises(sqlite3.OperationalError):
                    _apply_transactional_migration(
                        connection, lease, 29, statements, validate_before, validate_after,
                        fail_after_ledger)
            self.assertEqual(tuple(row[0] for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version")), tuple(range(1, 29)))
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='cp2b1_synthetic'").fetchone())

    def test_future_schema_and_partial_objects_fail_runtime_startup(self):
        data_dir = self.root / "unknown"
        data_dir.mkdir()
        source = data_dir / "nexo.sqlite3"
        create_historical_database(source, 28)
        with sqlite3.connect(source) as connection:
            connection.execute("INSERT INTO schema_migrations VALUES(29, 'future')")
        future_bytes = source.read_bytes()
        old_db = main.DB_PATH
        main.DB_PATH = source
        try:
            with self.assertRaises(SchemaCompatibilityError):
                main.startup()
            self.assertEqual(source.read_bytes(), future_bytes)

            partial = self.root / "partial"
            partial.mkdir()
            partial_path = partial / "nexo.sqlite3"
            create_historical_database(partial_path, 28)
            with sqlite3.connect(partial_path) as connection:
                connection.execute("DROP TABLE pending_approvals")
            main.DB_PATH = partial_path
            with self.assertRaises(SchemaCompatibilityError):
                main.startup()
        finally:
            main.shutdown()
            main.DB_PATH = old_db


if __name__ == "__main__":
    unittest.main()
