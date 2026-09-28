from __future__ import annotations

import sqlite3
from collections.abc import Callable


Migration = Callable[[sqlite3.Connection], None]


def _add_column_if_missing(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migration_1(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS providers (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
      api_key TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS models (
      id TEXT NOT NULL, provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
      label TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(provider_id,id)
    );
    CREATE TABLE IF NOT EXISTS conversations (
      id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
      role TEXT NOT NULL, content TEXT NOT NULL, provider_id TEXT, model_id TEXT,
      attachments TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS shadow_observations (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
      mode TEXT NOT NULL, model TEXT, answers TEXT NOT NULL DEFAULT '{}',
      execution TEXT NOT NULL DEFAULT '{}', metadata TEXT NOT NULL DEFAULT '{}',
      latency_ms REAL, error TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runtime_trace_events (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
      sequence INTEGER NOT NULL, type TEXT NOT NULL, started_at TEXT NOT NULL,
      duration_ms REAL, status TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS capability_refreshes (
      provider_id TEXT PRIMARY KEY REFERENCES providers(id) ON DELETE CASCADE,
      refreshed_at TEXT NOT NULL
    );
    """)
    _add_column_if_missing(connection, "models", "capabilities", "TEXT NOT NULL DEFAULT '[]'")
    _add_column_if_missing(connection, "messages", "sources", "TEXT NOT NULL DEFAULT '[]'")


MIGRATIONS: tuple[tuple[int, Migration], ...] = ((1, _migration_1),)


def _migration_2(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS runtime_runs (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
      message_id TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
      status TEXT NOT NULL, model TEXT, metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS runtime_events (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runtime_runs(id) ON DELETE CASCADE,
      sequence INTEGER NOT NULL, parent_event_id TEXT REFERENCES runtime_events(id),
      kind TEXT NOT NULL, name TEXT NOT NULL, started_at TEXT NOT NULL,
      completed_at TEXT, duration_ms REAL, status TEXT NOT NULL,
      safe_metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_runtime_runs_conversation ON runtime_runs(conversation_id, started_at);
    CREATE INDEX IF NOT EXISTS idx_runtime_runs_message ON runtime_runs(message_id);
    CREATE INDEX IF NOT EXISTS idx_runtime_events_run_sequence ON runtime_events(run_id, sequence);
    CREATE INDEX IF NOT EXISTS idx_runtime_events_started ON runtime_events(started_at);
    """)


MIGRATIONS = ((1, _migration_1), (2, _migration_2))


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    for version, migration in MIGRATIONS:
        if version in applied:
            continue
        migration(connection)
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, datetime('now'))", (version,))
