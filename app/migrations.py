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


def _migration_3(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS agent_profiles (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      provider_id TEXT NOT NULL,
      model_id TEXT NOT NULL,
      system_instructions TEXT NOT NULL DEFAULT '',
      model_parameters TEXT NOT NULL DEFAULT '{}',
      enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS agent_profile_tools (
      agent_profile_id TEXT NOT NULL REFERENCES agent_profiles(id) ON DELETE CASCADE,
      tool_name TEXT NOT NULL,
      PRIMARY KEY(agent_profile_id, tool_name)
    );
    CREATE INDEX IF NOT EXISTS idx_agent_profiles_name ON agent_profiles(name);
    """)


def _migration_4(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "conversations", "agent_profile_id", "TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_conversations_agent_profile ON conversations(agent_profile_id)")


def _migration_5(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS notebooks (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notebook_sources (
      id TEXT PRIMARY KEY,
      notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
      type TEXT NOT NULL CHECK(type IN ('file', 'web')),
      title TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('added', 'pending', 'extracting', 'ready', 'failed')),
      metadata TEXT NOT NULL DEFAULT '{}',
      content_hash TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_notebooks_updated ON notebooks(updated_at);
    CREATE INDEX IF NOT EXISTS idx_notebook_sources_notebook ON notebook_sources(notebook_id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_notebook_sources_hash ON notebook_sources(content_hash);
    """)


def _migration_6(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS canonical_documents (
      id TEXT PRIMARY KEY,
      notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
      source_id TEXT NOT NULL UNIQUE REFERENCES notebook_sources(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      content TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      content_type TEXT NOT NULL,
      language TEXT,
      metadata TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS canonical_spans (
      id TEXT PRIMARY KEY,
      document_id TEXT NOT NULL REFERENCES canonical_documents(id) ON DELETE CASCADE,
      start_offset INTEGER NOT NULL,
      end_offset INTEGER NOT NULL,
      source_type TEXT NOT NULL,
      source_location TEXT NOT NULL DEFAULT '{}',
      CHECK(start_offset >= 0 AND end_offset >= start_offset)
    );
    CREATE INDEX IF NOT EXISTS idx_canonical_documents_notebook ON canonical_documents(notebook_id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_canonical_spans_document ON canonical_spans(document_id, start_offset);
    """)
    _add_column_if_missing(connection, "notebook_sources", "error_code", "TEXT")
    _add_column_if_missing(connection, "notebook_sources", "error_message", "TEXT")
    _add_column_if_missing(connection, "notebook_sources", "adapter", "TEXT")
    _add_column_if_missing(connection, "notebook_sources", "extraction_duration_ms", "REAL")
    _add_column_if_missing(connection, "notebook_sources", "canonical_character_count", "INTEGER")


def _migration_7(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "messages", "runtime_metadata", "TEXT NOT NULL DEFAULT '{}'")


def _migration_8(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS document_chunks (
      id TEXT PRIMARY KEY,
      notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
      document_id TEXT NOT NULL REFERENCES canonical_documents(id) ON DELETE CASCADE,
      source_id TEXT NOT NULL REFERENCES notebook_sources(id) ON DELETE CASCADE,
      ordinal INTEGER NOT NULL,
      content TEXT NOT NULL,
      canonical_start INTEGER NOT NULL,
      canonical_end INTEGER NOT NULL,
      token_count INTEGER,
      metadata TEXT NOT NULL DEFAULT '{}',
      content_hash TEXT NOT NULL,
      embedding TEXT,
      embedding_dimension INTEGER,
      created_at TEXT NOT NULL,
      UNIQUE(document_id, ordinal),
      CHECK(canonical_start >= 0 AND canonical_end >= canonical_start)
    );
    CREATE INDEX IF NOT EXISTS idx_document_chunks_notebook ON document_chunks(notebook_id, document_id, ordinal);
    CREATE INDEX IF NOT EXISTS idx_document_chunks_source ON document_chunks(source_id);
    """)
    _add_column_if_missing(connection, "notebook_sources", "indexing_status", "TEXT NOT NULL DEFAULT 'not_indexed'")
    _add_column_if_missing(connection, "notebook_sources", "indexing_error", "TEXT")
    _add_column_if_missing(connection, "notebook_sources", "chunk_count", "INTEGER")
    _add_column_if_missing(connection, "notebook_sources", "embedding_batches", "INTEGER")
    _add_column_if_missing(connection, "notebook_sources", "embedding_duration_ms", "REAL")
    _add_column_if_missing(connection, "notebook_sources", "indexing_duration_ms", "REAL")
    _add_column_if_missing(connection, "notebook_sources", "indexed_at", "TEXT")


MIGRATIONS = ((1, _migration_1), (2, _migration_2), (3, _migration_3), (4, _migration_4), (5, _migration_5), (6, _migration_6), (7, _migration_7), (8, _migration_8))


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    for version, migration in MIGRATIONS:
        if version in applied:
            continue
        migration(connection)
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, datetime('now'))", (version,))
