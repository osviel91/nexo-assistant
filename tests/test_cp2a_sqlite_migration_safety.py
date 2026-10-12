import json
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import migrations
from prototypes.admin_persistence import BackupFailure, backup_database, restore_database
from prototypes.security_contracts import MIGRATION_REQUIREMENTS, activation_blockers
from prototypes.sqlite_migration_safety import (
    MigrationStateError,
    MigrationValidationError,
    apply_transactional_migration,
    capture_schema_manifest,
    check_database,
    validate_schema_manifest,
)


def connect(path: Path, *, isolation_level="DEFERRED") -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=isolation_level, timeout=5)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def database(path: Path):
    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def create_repository_schema(path: Path, through: int = 28) -> None:
    with database(path) as connection:
        selected = tuple(item for item in migrations.MIGRATIONS if item[0] <= through)
        with patch.object(migrations, "MIGRATIONS", selected):
            migrations.migrate(connection)


def require_candidate_schema(connection: sqlite3.Connection) -> None:
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(cp2a_records)"))
    if columns != ("id", "value"):
        raise MigrationValidationError("candidate schema does not match expected columns")
    if connection.execute("SELECT COUNT(*) FROM cp2a_records").fetchone()[0] < 1:
        raise MigrationValidationError("candidate schema/data postcondition failed")


def require_empty_schema(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables - {"schema_migrations"}:
        raise MigrationValidationError("unexpected objects before first migration")


def wait_for_child_marker(process: subprocess.Popen[str], expected: str) -> None:
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        if not selector.select(10):
            raise AssertionError(f"child did not reach synchronization point {expected!r}")
        marker = process.stdout.readline().strip()
        if marker != expected:
            raise AssertionError(f"expected child marker {expected!r}, got {marker!r}")
    finally:
        selector.close()


class SQLiteMigrationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_migrations_generate_selected_historical_schemas_and_upgrade_to_v28(self):
        for version in (1, 12, 21, 27, 28):
            with self.subTest(version=version):
                path = self.root / f"history-{version}.sqlite3"
                create_repository_schema(path, version)
                with database(path) as connection:
                    self.assertEqual(check_database(connection).migration_versions, tuple(range(1, version + 1)))
                    connection.execute("INSERT INTO providers VALUES('cp2a','synthetic','https://invalid/','','fixture')")
                    if version < 28:
                        migrations.migrate(connection)
                    self.assertEqual(check_database(connection).migration_versions, tuple(range(1, 29)))
                    self.assertEqual(connection.execute("SELECT name FROM providers WHERE id='cp2a'").fetchone()[0],
                                     "synthetic")
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_current_runner_executescript_sql_error_leaves_objects_and_data_without_ledger_row(self):
        path = self.root / "executescript-failure.sqlite3"

        def broken_migration(connection):
            connection.executescript("""
                CREATE TABLE cp2a_partial_parent(id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE cp2a_partial_child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES cp2a_partial_parent(id));
                INSERT INTO cp2a_partial_parent VALUES(1, 'synthetic');
                INSERT INTO cp2a_missing_table VALUES(1);
            """)

        with database(path) as connection, patch.object(migrations, "MIGRATIONS", ((1, broken_migration),)):
            with self.assertRaises(sqlite3.OperationalError):
                migrations.migrate(connection)

        with database(path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("cp2a_partial_parent", tables)
            self.assertIn("cp2a_partial_child", tables)
            self.assertEqual(connection.execute("SELECT value FROM cp2a_partial_parent").fetchone()[0], "synthetic")
            self.assertEqual(check_database(connection).migration_versions, ())
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        def idempotent_retry(connection):
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS cp2a_partial_parent(id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cp2a_partial_child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES cp2a_partial_parent(id));
                INSERT OR IGNORE INTO cp2a_partial_parent VALUES(1, 'synthetic');
            """)

        with database(path) as connection, patch.object(migrations, "MIGRATIONS", ((1, idempotent_retry),)):
            migrations.migrate(connection)
            self.assertEqual(check_database(connection).migration_versions, (1,))
            migrations.migrate(connection)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cp2a_partial_parent").fetchone()[0], 1)

    def test_executescript_commits_a_pending_outer_transaction_before_running_script(self):
        path = self.root / "executescript-outer-transaction.sqlite3"
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE cp2a_before_script(value TEXT)")
        connection.execute("INSERT INTO cp2a_before_script VALUES('outer transaction')")
        connection.executescript("""
            CREATE TABLE cp2a_inside_script(value TEXT);
            INSERT INTO cp2a_inside_script VALUES('script statement');
        """)
        self.assertFalse(connection.in_transaction)
        connection.rollback()
        connection.close()

        with database(path) as reopened:
            self.assertEqual(reopened.execute("SELECT value FROM cp2a_before_script").fetchone()[0],
                             "outer transaction")
            self.assertEqual(reopened.execute("SELECT value FROM cp2a_inside_script").fetchone()[0],
                             "script statement")

    def test_connection_context_rolls_back_dml_but_not_prior_implicit_ddl(self):
        path = self.root / "mixed-ddl-dml.sqlite3"

        def fails_after_execute(connection):
            connection.execute("CREATE TABLE cp2a_mixed(value TEXT)")
            connection.execute("INSERT INTO cp2a_mixed VALUES('uncommitted dml')")
            raise RuntimeError("injected migration stop before ledger")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            with database(path) as connection, patch.object(migrations, "MIGRATIONS", ((1, fails_after_execute),)):
                migrations.migrate(connection)

        with database(path) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("cp2a_mixed", names)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cp2a_mixed").fetchone()[0], 0)
            self.assertEqual(check_database(connection).migration_versions, ())

    def test_current_runner_does_not_reject_future_versions_gaps_or_unexpected_schema(self):
        path = self.root / "ledger-drift.sqlite3"
        create_repository_schema(path)
        with database(path) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=12")
            connection.execute("INSERT INTO schema_migrations VALUES(29, 'future fixture')")
            connection.execute("CREATE TABLE cp2a_unexpected(value TEXT)")
            migrations.migrate(connection)
            versions = check_database(connection).migration_versions
            self.assertEqual(versions, tuple(range(1, 30)))
            self.assertIn("cp2a_unexpected", {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")})

    def test_current_runner_accepts_v28_ledger_even_when_a_required_table_is_missing(self):
        path = self.root / "missing-object-v28.sqlite3"
        create_repository_schema(path)
        with database(path) as connection:
            connection.execute("DROP TABLE pending_approvals")
            migrations.migrate(connection)
            self.assertEqual(check_database(connection).migration_versions, tuple(range(1, 29)))
            self.assertIsNone(connection.execute("""SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='pending_approvals'""").fetchone())

    def test_current_runner_competing_connections_can_both_observe_a_pending_migration(self):
        path = self.root / "runner-concurrent.sqlite3"
        arrived = threading.Barrier(2)

        def migration(connection):
            arrived.wait(timeout=10)
            connection.execute("CREATE TABLE IF NOT EXISTS cp2a_concurrent(id INTEGER PRIMARY KEY)")
            connection.execute("INSERT OR IGNORE INTO cp2a_concurrent VALUES(1)")

        def run_migrate():
            try:
                with database(path) as connection:
                    migrations.migrate(connection)
                return "committed"
            except sqlite3.Error:
                return "failed"

        with patch.object(migrations, "MIGRATIONS", ((1, migration),)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _index: run_migrate(), range(2)))
        self.assertCountEqual(outcomes, ("committed", "failed"))
        with database(path) as connection:
            self.assertEqual(check_database(connection).migration_versions, (1,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cp2a_concurrent").fetchone()[0], 1)

    def test_candidate_rolls_back_each_fault_boundary_then_retries_and_is_idempotent(self):
        stages = ("before_migration", "after_statement:1", "after_statement:2",
                  "before_ledger", "after_ledger")
        statements = (
            "CREATE TABLE cp2a_records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO cp2a_records VALUES(1, 'synthetic')",
        )
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                path = self.root / f"candidate-fault-{index}.sqlite3"
                connection = connect(path, isolation_level=None)

                def fault(actual, _connection):
                    if actual == stage:
                        raise sqlite3.OperationalError("injected fixture failure")

                with self.assertRaises(sqlite3.OperationalError):
                    apply_transactional_migration(connection, 1, statements, require_empty_schema,
                                                  require_candidate_schema, fault)
                self.assertFalse(connection.in_transaction)
                self.assertNotIn("schema_migrations", {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")})
                self.assertNotIn("cp2a_records", {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")})
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(apply_transactional_migration(
                    connection, 1, statements, require_empty_schema, require_candidate_schema), True)
                self.assertEqual(apply_transactional_migration(
                    connection, 1, statements, require_empty_schema, require_candidate_schema), False)
                self.assertEqual(check_database(connection).migration_versions, (1,))
                connection.close()

    def test_candidate_concurrent_migrators_serialize_on_begin_immediate(self):
        path = self.root / "candidate-concurrent.sqlite3"
        start = threading.Barrier(2)
        statements = (
            "CREATE TABLE cp2a_records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO cp2a_records VALUES(1, 'synthetic')",
        )

        def apply():
            connection = connect(path, isolation_level=None)
            try:
                start.wait(timeout=10)
                return apply_transactional_migration(connection, 1, statements,
                                                      require_empty_schema, require_candidate_schema)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: apply(), range(2)))
        self.assertCountEqual(outcomes, (True, False))
        with database(path) as connection:
            self.assertEqual(check_database(connection).migration_versions, (1,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cp2a_records").fetchone()[0], 1)

    def test_candidate_sql_error_mid_migration_rolls_back_schema_data_and_ledger(self):
        connection = connect(self.root / "candidate-sql-error.sqlite3", isolation_level=None)
        with self.assertRaises(sqlite3.OperationalError):
            apply_transactional_migration(connection, 1, (
                "CREATE TABLE cp2a_records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)",
                "INSERT INTO cp2a_records VALUES(1, 'synthetic')",
                "INSERT INTO cp2a_missing VALUES(1)",
            ), require_empty_schema, require_candidate_schema)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(check_database(connection).migration_versions, ())
        self.assertEqual({row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}, set())
        connection.close()

    def test_candidate_rejects_gap_future_ledger_wrong_schema_and_disabled_foreign_keys(self):
        statements = (
            "CREATE TABLE cp2a_records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO cp2a_records VALUES(1, 'synthetic')",
        )
        gap = connect(self.root / "candidate-gap.sqlite3", isolation_level=None)
        gap.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        gap.execute("INSERT INTO schema_migrations VALUES(2, 'fixture')")
        with self.assertRaises(MigrationStateError):
            apply_transactional_migration(gap, 3, statements, require_empty_schema, require_candidate_schema)
        gap.close()

        future = connect(self.root / "candidate-future.sqlite3", isolation_level=None)
        future.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        future.execute("INSERT INTO schema_migrations VALUES(2, 'fixture')")
        with self.assertRaises(MigrationStateError):
            apply_transactional_migration(future, 1, statements, require_empty_schema, require_candidate_schema)
        future.close()

        malformed = connect(self.root / "candidate-malformed-ledger.sqlite3", isolation_level=None)
        malformed.execute("CREATE TABLE schema_migrations(version TEXT, applied_at TEXT NOT NULL)")
        with self.assertRaises(MigrationStateError):
            apply_transactional_migration(malformed, 1, statements, require_empty_schema, require_candidate_schema)
        malformed.close()

        partial = connect(self.root / "candidate-partial.sqlite3", isolation_level=None)
        partial.execute("CREATE TABLE cp2a_records(wrong TEXT, extra TEXT)")
        with self.assertRaises(MigrationValidationError):
            apply_transactional_migration(partial, 1, statements, require_empty_schema, require_candidate_schema)
        self.assertEqual(check_database(partial).migration_versions, ())
        self.assertEqual(tuple(row[1] for row in partial.execute("PRAGMA table_info(cp2a_records)")),
                         ("wrong", "extra"))
        partial.close()

        no_fk = sqlite3.connect(self.root / "candidate-no-fk.sqlite3", isolation_level=None)
        with self.assertRaises(MigrationStateError):
            apply_transactional_migration(no_fk, 1, statements, require_empty_schema, require_candidate_schema)
        no_fk.close()

    def test_candidate_rejects_foreign_key_orphan_in_the_pre_migration_state(self):
        path = self.root / "candidate-orphan.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE cp2a_parent(id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE cp2a_child(parent_id INTEGER REFERENCES cp2a_parent(id))")
            connection.execute("INSERT INTO cp2a_child VALUES(404)")

        connection = connect(path, isolation_level=None)

        def validate_previous(db):
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"cp2a_parent", "cp2a_child"}.issubset(tables):
                raise MigrationValidationError("fixture parent schema missing")

        def validate_next(db):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cp2a_records'").fetchone():
                raise MigrationValidationError("fixture next schema missing")

        with self.assertRaises(MigrationValidationError):
            apply_transactional_migration(connection, 1,
                                           ("CREATE TABLE cp2a_records(value TEXT)",),
                                           validate_previous, validate_next)
        self.assertEqual(check_database(connection).migration_versions, ())
        self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='cp2a_records'").fetchone())
        connection.close()

    def test_schema_manifest_checks_constraints_indexes_foreign_keys_and_explicit_extensions(self):
        good = self.root / "manifest-good.sqlite3"
        with database(good) as connection:
            connection.executescript("""
                CREATE TABLE cp2a_parent(
                    id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE CHECK(length(code) > 0),
                    normalized TEXT GENERATED ALWAYS AS (lower(code)) STORED
                );
                CREATE TABLE cp2a_record(
                    id INTEGER PRIMARY KEY,
                    parent_id INTEGER NOT NULL REFERENCES cp2a_parent(id) ON DELETE CASCADE,
                    value TEXT NOT NULL CHECK(length(value) < 20),
                    UNIQUE(parent_id, value)
                );
                CREATE INDEX idx_cp2a_record_value ON cp2a_record(value);
                CREATE TRIGGER cp2a_record_guard BEFORE INSERT ON cp2a_record
                    WHEN NEW.value = 'blocked' BEGIN SELECT RAISE(ABORT, 'blocked'); END;
                CREATE VIEW cp2a_readonly_extension AS SELECT id, code FROM cp2a_parent;
            """)
            manifest = capture_schema_manifest(connection, {("view", "cp2a_readonly_extension")})
            validate_schema_manifest(connection, manifest)

            record = next(table for table in manifest.tables if table.name == "cp2a_record")
            self.assertEqual(tuple(column[0] for column in record.columns), ("id", "parent_id", "value"))
            self.assertEqual(record.columns[0][4], 1)
            self.assertEqual(record.columns[1][2], 1)
            parent = next(table for table in manifest.tables if table.name == "cp2a_parent")
            self.assertEqual(parent.columns[-1][0], "normalized")
            self.assertEqual(parent.columns[-1][5], 3)
            self.assertIn("idx_cp2a_record_value", {index[0] for index in record.indexes})
            self.assertEqual(record.foreign_keys[0][2:7], ("cp2a_parent", "parent_id", "id", "NO ACTION", "CASCADE"))
            ddl = next(item.sql for item in manifest.objects if item.name == "cp2a_record")
            self.assertIn("check(length(value) < 20)", ddl.casefold())
            self.assertIn("unique(parent_id, value)", ddl.casefold())
            self.assertTrue(any(item.name == "cp2a_readonly_extension" for item in manifest.permitted_extensions))

            connection.execute("CREATE TABLE cp2a_unlisted_extension(value TEXT)")
            with self.assertRaises(MigrationValidationError):
                validate_schema_manifest(connection, manifest)

        changed_constraint = self.root / "manifest-changed-constraint.sqlite3"
        with database(changed_constraint) as connection:
            connection.execute("""CREATE TABLE cp2a_parent(
                id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE CHECK(length(code) > 0),
                normalized TEXT GENERATED ALWAYS AS (lower(code)) STORED)""")
            connection.execute("""CREATE TABLE cp2a_record(
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES cp2a_parent(id) ON DELETE CASCADE,
                value TEXT NOT NULL CHECK(length(value) <= 20),
                UNIQUE(parent_id, value))""")
            connection.execute("CREATE INDEX idx_cp2a_record_value ON cp2a_record(value)")
            connection.execute("""CREATE TRIGGER cp2a_record_guard BEFORE INSERT ON cp2a_record
                WHEN NEW.value = 'blocked' BEGIN SELECT RAISE(ABORT, 'blocked'); END""")
            connection.execute("CREATE VIEW cp2a_readonly_extension AS SELECT id, code FROM cp2a_parent")
            with self.assertRaises(MigrationValidationError):
                validate_schema_manifest(connection, manifest)

        changed_extension = self.root / "manifest-changed-extension.sqlite3"
        with database(changed_extension) as connection:
            connection.executescript("""
                CREATE TABLE cp2a_parent(
                    id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE CHECK(length(code) > 0),
                    normalized TEXT GENERATED ALWAYS AS (lower(code)) STORED);
                CREATE TABLE cp2a_record(
                    id INTEGER PRIMARY KEY,
                    parent_id INTEGER NOT NULL REFERENCES cp2a_parent(id) ON DELETE CASCADE,
                    value TEXT NOT NULL CHECK(length(value) < 20), UNIQUE(parent_id, value));
                CREATE INDEX idx_cp2a_record_value ON cp2a_record(value);
                CREATE TRIGGER cp2a_record_guard BEFORE INSERT ON cp2a_record
                    WHEN NEW.value = 'blocked' BEGIN SELECT RAISE(ABORT, 'blocked'); END;
                CREATE VIEW cp2a_readonly_extension AS SELECT id FROM cp2a_parent;
            """)
            with self.assertRaises(MigrationValidationError):
                validate_schema_manifest(connection, manifest)

    def test_candidate_uses_structural_manifests_before_and_after_ledgered_ddl(self):
        path = self.root / "manifest-candidate.sqlite3"
        expected_path = self.root / "manifest-candidate-expected.sqlite3"
        for fixture, with_index in ((path, False), (expected_path, True)):
            with database(fixture) as connection:
                connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                connection.execute("INSERT INTO schema_migrations VALUES(1, 'fixture')")
                connection.execute("CREATE TABLE cp2a_record(id INTEGER PRIMARY KEY, value TEXT NOT NULL CHECK(length(value) > 0))")
                connection.execute("CREATE VIEW cp2a_extension AS SELECT id,value FROM cp2a_record")
                if with_index:
                    connection.execute("CREATE INDEX idx_cp2a_value ON cp2a_record(value)")

        with database(path) as connection, database(expected_path) as expected_connection:
            permitted = {("view", "cp2a_extension")}
            previous_manifest = capture_schema_manifest(connection, permitted)
            next_manifest = capture_schema_manifest(expected_connection, permitted)
            self.assertTrue(apply_transactional_migration(
                connection, 2, ("CREATE INDEX idx_cp2a_value ON cp2a_record(value)",),
                lambda db: validate_schema_manifest(db, previous_manifest),
                lambda db: validate_schema_manifest(db, next_manifest)))
            self.assertEqual(check_database(connection).migration_versions, (1, 2))
            connection.execute("DROP INDEX idx_cp2a_value")
            with self.assertRaises(MigrationValidationError):
                apply_transactional_migration(
                    connection, 2, (), lambda _db: None,
                    lambda db: validate_schema_manifest(db, next_manifest))

    def test_bootstrap_fixture_failure_never_sets_initialized_or_creates_auth_tables(self):
        path = self.root / "bootstrap-fixture.sqlite3"
        connection = connect(path, isolation_level=None)
        connection.execute("CREATE TABLE cp2a_fixture_state(initialized INTEGER NOT NULL CHECK(initialized IN (0,1)))")
        connection.execute("INSERT INTO cp2a_fixture_state VALUES(0)")
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES(1, 'fixture baseline')")
        operations = (
            "CREATE TABLE cp2a_fixture_identity(id TEXT PRIMARY KEY)",
            "INSERT INTO cp2a_fixture_identity VALUES('synthetic-admin')",
            "UPDATE cp2a_fixture_state SET initialized=1",
        )

        def validate_bootstrap(db):
            if db.execute("SELECT initialized FROM cp2a_fixture_state").fetchone()[0] != 1:
                raise MigrationValidationError("fixture bootstrap incomplete")
            if db.execute("SELECT COUNT(*) FROM cp2a_fixture_identity").fetchone()[0] != 1:
                raise MigrationValidationError("fixture identity missing")

        def validate_uninitialized_fixture(db):
            if db.execute("SELECT initialized FROM cp2a_fixture_state").fetchone()[0] != 0:
                raise MigrationValidationError("bootstrap fixture is not uninitialized")

        with self.assertRaises(sqlite3.OperationalError):
            apply_transactional_migration(connection, 2, operations, validate_uninitialized_fixture,
                                          validate_bootstrap,
                                          lambda stage, _db: (_ for _ in ()).throw(sqlite3.OperationalError("injected"))
                                          if stage == "after_statement:2" else None)
        self.assertEqual(connection.execute("SELECT initialized FROM cp2a_fixture_state").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'auth_%'").fetchone()[0], 0)
        self.assertEqual(check_database(connection).migration_versions, (1,))
        connection.close()

    @unittest.skipUnless(os.name == "posix", "SIGKILL semantics are only asserted on POSIX")
    def test_sigkill_current_runner_leaves_committed_schema_without_ledger_then_retry_repairs_fixture(self):
        for journal in ("delete", "wal"):
            with self.subTest(journal=journal):
                path = self.root / f"runner-sigkill-{journal}.sqlite3"
                script = """
import sqlite3, sys
from app import migrations
def paused(connection):
    connection.executescript("CREATE TABLE cp2a_killed(id INTEGER PRIMARY KEY); INSERT INTO cp2a_killed VALUES(1);")
    print('READY', flush=True)
    sys.stdin.read(1)
migrations.MIGRATIONS = ((1, paused),)
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=' + sys.argv[2])
migrations.migrate(connection)
"""
                process = subprocess.Popen([sys.executable, "-c", script, str(path), journal],
                                           cwd=Path(__file__).parents[1], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                try:
                    wait_for_child_marker(process, "READY")
                    process.send_signal(signal.SIGKILL)
                    self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                    process.communicate(timeout=10)

                with database(path) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM cp2a_killed").fetchone()[0], 1)
                    self.assertEqual(check_database(connection).migration_versions, ())
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

                def idempotent_retry(connection):
                    connection.executescript("""
                        CREATE TABLE IF NOT EXISTS cp2a_killed(id INTEGER PRIMARY KEY);
                        INSERT OR IGNORE INTO cp2a_killed VALUES(1);
                    """)

                with database(path) as connection, patch.object(migrations, "MIGRATIONS", ((1, idempotent_retry),)):
                    migrations.migrate(connection)
                    self.assertEqual(check_database(connection).migration_versions, (1,))
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM cp2a_killed").fetchone()[0], 1)

    @unittest.skipUnless(os.name == "posix", "SIGKILL semantics are only asserted on POSIX")
    def test_sigkill_candidate_before_commit_rolls_back_and_after_commit_is_complete(self):
        for journal in ("delete", "wal"):
            for target in ("after_statement:1", "after_ledger", "after_commit"):
                with self.subTest(journal=journal, target=target):
                    path = self.root / f"candidate-sigkill-{journal}-{target.replace(':', '-')}.sqlite3"
                    script = """
import sqlite3, sys
from prototypes.sqlite_migration_safety import apply_transactional_migration
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA foreign_keys=ON')
connection.execute('PRAGMA journal_mode=' + sys.argv[3])
target = sys.argv[2]
def fault(stage, _connection):
    if stage == target:
        print('READY', flush=True)
        sys.stdin.read(1)
def validate(db):
    cols = tuple(row[1] for row in db.execute('PRAGMA table_info(cp2a_records)'))
    if cols != ('id', 'value') or db.execute('SELECT COUNT(*) FROM cp2a_records').fetchone()[0] != 1:
        raise RuntimeError('invalid candidate state')
def validate_previous(db):
    tables = {row[0] for row in db.execute('SELECT name FROM sqlite_master WHERE type="table"')}
    if tables - {'schema_migrations'}:
        raise RuntimeError('unexpected previous schema')
statements = ('CREATE TABLE cp2a_records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)',
              "INSERT INTO cp2a_records VALUES(1, 'synthetic')")
if target == 'after_commit':
    apply_transactional_migration(connection, 1, statements, validate_previous, validate)
    print('READY', flush=True)
    sys.stdin.read(1)
else:
    apply_transactional_migration(connection, 1, statements, validate_previous, validate, fault)
"""
                    process = subprocess.Popen([sys.executable, "-c", script, str(path), target, journal],
                                               cwd=Path(__file__).parents[1], stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                    try:
                        wait_for_child_marker(process, "READY")
                        process.send_signal(signal.SIGKILL)
                        self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=10)
                        process.communicate(timeout=10)
                    with database(path) as connection:
                        check = check_database(connection)
                        if target == "after_commit":
                            self.assertEqual(check.migration_versions, (1,))
                            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cp2a_records").fetchone()[0], 1)
                        else:
                            self.assertEqual(check.migration_versions, ())
                            tables = {row[0] for row in connection.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'")}
                            self.assertNotIn("schema_migrations", tables)
                            self.assertNotIn("cp2a_records", tables)
                        self.assertEqual(check.integrity, "ok")
                        self.assertEqual(check.foreign_key_violations, ())
                    if target != "after_commit":
                        connection = connect(path, isolation_level=None)
                        self.assertTrue(apply_transactional_migration(
                            connection, 1,
                            ("CREATE TABLE cp2a_records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)",
                             "INSERT INTO cp2a_records VALUES(1, 'synthetic')"),
                            require_empty_schema, require_candidate_schema))
                        connection.close()
                        with database(path) as recovered:
                            self.assertEqual(check_database(recovered).migration_versions, (1,))

    def test_backup_api_preserves_v28_schema_ledger_and_data_during_wal_writer_commit(self):
        source = self.root / "nexo-v28.sqlite3"
        backup = self.root / "nexo-v28-backup.sqlite3"
        restored = self.root / "nexo-v28-restored.sqlite3"
        create_repository_schema(source)
        with database(source) as connection:
            connection.execute("INSERT INTO providers VALUES('cp2a','synthetic','https://invalid/','','fixture')")
            connection.execute("CREATE TABLE cp2a_backup_rows(generation INTEGER, ordinal INTEGER, payload TEXT, PRIMARY KEY(generation,ordinal))")
            connection.execute("CREATE TABLE cp2a_wal_marker(value TEXT)")
            connection.executemany("INSERT INTO cp2a_backup_rows VALUES(1,?,?)",
                                   ((i, "g1:" + "a" * 1024) for i in range(700)))
        with database(source) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        wal_keeper = connect(source, isolation_level=None)
        self.addCleanup(wal_keeper.close)
        wal_keeper.execute("PRAGMA wal_autocheckpoint=0")
        wal_keeper.execute("INSERT INTO cp2a_wal_marker VALUES('committed in WAL')")
        self.assertTrue(Path(str(source) + "-wal").exists())

        writer_done = []
        writer_future = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            def writer():
                connection = connect(source, isolation_level=None)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("INSERT INTO cp2a_backup_rows VALUES(2,0,?)", ("g2:" + "b" * 1024,))
                    connection.execute("DELETE FROM cp2a_backup_rows WHERE generation=1")
                    connection.commit()
                    writer_done.append(True)
                finally:
                    connection.close()

            def progress(_status, remaining, total):
                nonlocal writer_future
                if writer_future is None and remaining < total:
                    writer_future = pool.submit(writer)
                    writer_future.result(timeout=10)

            self.assertEqual(backup_database(source, backup, pages=8, progress=progress),
                             {"integrity": "ok", "foreign_key_violations": 0})
            self.assertIsNotNone(writer_future)

        self.assertEqual(writer_done, [True])
        with database(source) as source_db, database(backup) as backup_db:
            self.assertEqual(check_database(source_db).migration_versions, tuple(range(1, 29)))
            self.assertEqual(check_database(backup_db).migration_versions, tuple(range(1, 29)))
            self.assertEqual(source_db.execute("SELECT COUNT(*) FROM providers WHERE id='cp2a'").fetchone()[0], 1)
            self.assertEqual(backup_db.execute("SELECT value FROM cp2a_wal_marker").fetchone()[0],
                             "committed in WAL")
            rows = backup_db.execute("SELECT generation, COUNT(*), MIN(payload), MAX(payload) FROM cp2a_backup_rows GROUP BY generation").fetchall()
            self.assertEqual(len(rows), 1)
            generation, count, first, last = rows[0]
            self.assertEqual(first[:3], f"g{generation}:")
            self.assertEqual(last[:3], f"g{generation}:")
            self.assertIn((generation, count), ((1, 700), (2, 1)))
            self.assertEqual(backup_db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup_db.execute("PRAGMA foreign_key_check").fetchall(), [])

        self.assertEqual(restore_database(backup, restored), {"integrity": "ok", "foreign_key_violations": 0})
        with database(restored) as restored_db:
            self.assertEqual(check_database(restored_db).migration_versions, tuple(range(1, 29)))
            self.assertEqual(restored_db.execute("SELECT COUNT(*) FROM providers WHERE id='cp2a'").fetchone()[0], 1)

    def test_backup_failure_keeps_source_and_allows_retry_to_new_destination(self):
        source = self.root / "backup-source.sqlite3"
        target = self.root / "backup-target.sqlite3"
        create_repository_schema(source)
        with database(source) as connection:
            connection.execute("CREATE TABLE cp2a_payload(value TEXT)")
            connection.executemany("INSERT INTO cp2a_payload VALUES(?)", ((str(i) * 500,) for i in range(300)))
        before = source.read_bytes()

        def fail_during_backup(_status, remaining, total):
            if remaining < total:
                raise RuntimeError("injected temporary backup failure")

        with self.assertRaises(BackupFailure):
            backup_database(source, target, pages=1, progress=fail_during_backup)
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(backup_database(source, target)["integrity"], "ok")
        with database(target) as connection:
            self.assertEqual(check_database(connection).migration_versions, tuple(range(1, 29)))

    def test_integrity_valid_but_incompatible_restore_is_rejected_by_schema_gate(self):
        source = self.root / "v27.sqlite3"
        backup = self.root / "v27-backup.sqlite3"
        restored = self.root / "v27-restored.sqlite3"
        create_repository_schema(source, 27)
        backup_database(source, backup)
        restore_database(backup, restored)
        with database(restored) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(check_database(connection).migration_versions, tuple(range(1, 28)))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(mcp_tools)")}
            self.assertNotIn("read_only_hint", columns)
            with self.assertRaises(MigrationValidationError):
                if check_database(connection).migration_versions != tuple(range(1, 29)) or "read_only_hint" not in columns:
                    raise MigrationValidationError("restored database is incompatible with required v28 schema")

    def test_main_db_pragmas_are_measured_on_temporary_path(self):
        script = """
import json, sqlite3
from app.main import db
connection=db()
print(json.dumps({'sqlite':sqlite3.sqlite_version,
 'journal':connection.execute('PRAGMA journal_mode').fetchone()[0],
 'foreign_keys':connection.execute('PRAGMA foreign_keys').fetchone()[0],
 'busy_timeout':connection.execute('PRAGMA busy_timeout').fetchone()[0]}))
connection.close()
"""
        data_dir = self.root / "runtime-data"
        env = os.environ.copy() | {"NEXO_DATA_DIR": str(data_dir), "NEXO_MODULES": ""}
        result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True,
                                text=True, env=env, cwd=Path(__file__).parents[1])
        self.assertEqual(json.loads(result.stdout), {
            "sqlite": sqlite3.sqlite_version, "journal": "delete", "foreign_keys": 1, "busy_timeout": 5000,
        })
        self.assertEqual({item.name for item in data_dir.iterdir()}, {"notebook-sources", "nexo.sqlite3"})

    def test_cp1_migration_gates_remain_blocked_by_prototype_only_evidence(self):
        evidence = {requirement: "prototype-verified" for requirement in MIGRATION_REQUIREMENTS}
        self.assertTrue(set(MIGRATION_REQUIREMENTS).issubset(activation_blockers(evidence)))


if __name__ == "__main__":
    unittest.main()
