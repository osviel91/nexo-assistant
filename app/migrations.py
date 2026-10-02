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


def _migration_9(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "conversations", "notebook_id", "TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_conversations_notebook ON conversations(notebook_id)")
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS message_citations (
      id TEXT PRIMARY KEY,
      message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
      citation_key TEXT NOT NULL,
      notebook_id TEXT NOT NULL,
      source_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      chunk_id TEXT NOT NULL,
      canonical_start INTEGER NOT NULL,
      canonical_end INTEGER NOT NULL,
      provenance TEXT NOT NULL DEFAULT '[]',
      document_content_hash TEXT,
      chunk_content_hash TEXT,
      UNIQUE(message_id, citation_key)
    );
    CREATE INDEX IF NOT EXISTS idx_message_citations_message ON message_citations(message_id);
    """)


def _migration_10(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS embedding_configurations (
      id TEXT PRIMARY KEY,
      provider_id TEXT NOT NULL REFERENCES providers(id),
      model_id TEXT NOT NULL,
      target_chunk_size INTEGER NOT NULL,
      max_chunk_size INTEGER NOT NULL,
      overlap INTEGER NOT NULL,
      batch_size INTEGER NOT NULL,
      retrieval_top_k INTEGER NOT NULL,
      retrieval_max_context_chars INTEGER NOT NULL,
      config_version INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vector_index_identities (
      document_id TEXT PRIMARY KEY REFERENCES canonical_documents(id) ON DELETE CASCADE,
      provider_id TEXT NOT NULL,
      model_id TEXT NOT NULL,
      dimension INTEGER NOT NULL,
      embedding_config_version INTEGER NOT NULL,
      chunking_config_hash TEXT NOT NULL,
      document_hash TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    """)
    _add_column_if_missing(connection, "notebook_sources", "embedding_count", "INTEGER")
    _add_column_if_missing(connection, "notebook_sources", "vector_count", "INTEGER")
    _add_column_if_missing(connection, "notebook_sources", "embedding_dimension", "INTEGER")
    _add_column_if_missing(connection, "notebook_sources", "embedding_config_version", "INTEGER")
    _add_column_if_missing(connection, "notebook_sources", "indexing_identity", "TEXT")


def _migration_11(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "conversations", "execution_mode", "TEXT NOT NULL DEFAULT 'chat'")
    connection.execute("UPDATE conversations SET execution_mode='agent' WHERE agent_profile_id IS NOT NULL AND execution_mode='chat'")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC, id DESC)")


def _migration_12(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
      chunk_id UNINDEXED, notebook_id UNINDEXED, source_id UNINDEXED, content
    );
    INSERT INTO document_chunks_fts(chunk_id, notebook_id, source_id, content)
      SELECT dc.id, dc.notebook_id, dc.source_id, dc.content
      FROM document_chunks dc
      WHERE NOT EXISTS (SELECT 1 FROM document_chunks_fts fts WHERE fts.chunk_id=dc.id);
    """)
    _add_column_if_missing(connection, "embedding_configurations", "retrieval_mode", "TEXT NOT NULL DEFAULT 'hybrid'")
    _add_column_if_missing(connection, "embedding_configurations", "dense_candidate_limit", "INTEGER NOT NULL DEFAULT 20")
    _add_column_if_missing(connection, "embedding_configurations", "lexical_candidate_limit", "INTEGER NOT NULL DEFAULT 20")
    _add_column_if_missing(connection, "embedding_configurations", "rrf_k", "INTEGER NOT NULL DEFAULT 60")
    _add_column_if_missing(connection, "embedding_configurations", "final_top_k", "INTEGER NOT NULL DEFAULT 5")
    connection.execute("UPDATE embedding_configurations SET final_top_k=retrieval_top_k WHERE final_top_k IS NULL OR final_top_k=5 AND retrieval_top_k != 5")
    connection.execute("UPDATE conversations SET execution_mode='agent' WHERE agent_profile_id IS NOT NULL AND execution_mode='chat'")


def _migration_13(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE conversations SET execution_mode='agent' WHERE agent_profile_id IS NOT NULL AND execution_mode='chat'")
    connection.execute("""INSERT INTO document_chunks_fts(chunk_id,notebook_id,source_id,content)
        SELECT dc.id, dc.notebook_id, dc.source_id, dc.content FROM document_chunks dc
        WHERE NOT EXISTS (SELECT 1 FROM document_chunks_fts fts WHERE fts.chunk_id=dc.id)""")
    _add_column_if_missing(connection, "embedding_configurations", "reranking_enabled", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(connection, "embedding_configurations", "reranker_provider_id", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "embedding_configurations", "reranker_model", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "embedding_configurations", "reranker_candidate_limit", "INTEGER NOT NULL DEFAULT 8")
    _add_column_if_missing(connection, "embedding_configurations", "reranker_timeout_ms", "INTEGER NOT NULL DEFAULT 3000")


def _migration_14(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM document_chunks_fts WHERE chunk_id NOT IN (SELECT id FROM document_chunks)")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS document_chunks_fts_after_delete
        AFTER DELETE ON document_chunks
        BEGIN
          DELETE FROM document_chunks_fts WHERE chunk_id=OLD.id;
        END""")


def _migration_15(connection: sqlite3.Connection) -> None:
    connection.execute("""INSERT INTO document_chunks_fts(chunk_id,notebook_id,source_id,content)
        SELECT dc.id,dc.notebook_id,dc.source_id,dc.content FROM document_chunks dc
        WHERE NOT EXISTS (SELECT 1 FROM document_chunks_fts fts WHERE fts.chunk_id=dc.id)""")
    connection.execute("DELETE FROM document_chunks_fts WHERE chunk_id NOT IN (SELECT id FROM document_chunks)")
    connection.execute("""DELETE FROM vector_index_identities
        WHERE document_id NOT IN (SELECT document_id FROM document_chunks)""")
    connection.execute("""UPDATE notebook_sources SET indexing_status='not_indexed', indexing_error=NULL,
        chunk_count=NULL, embedding_count=NULL, vector_count=NULL, embedding_dimension=NULL,
        embedding_config_version=NULL, indexing_identity=NULL, indexed_at=NULL
        WHERE indexing_status='ready' AND (NOT EXISTS (
            SELECT 1 FROM canonical_documents cd WHERE cd.source_id=notebook_sources.id
        ) OR NOT EXISTS (
            SELECT 1 FROM document_chunks dc WHERE dc.source_id=notebook_sources.id
        ))""")


def _migration_16(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "embedding_configurations", "relevance_gate_enabled", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(connection, "embedding_configurations", "relevance_gate_min_term_overlap", "INTEGER NOT NULL DEFAULT 1")


MIGRATIONS = ((1, _migration_1), (2, _migration_2), (3, _migration_3), (4, _migration_4), (5, _migration_5), (6, _migration_6), (7, _migration_7), (8, _migration_8), (9, _migration_9), (10, _migration_10), (11, _migration_11), (12, _migration_12), (13, _migration_13), (14, _migration_14), (15, _migration_15), (16, _migration_16))


def _migration_17(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "messages", "artifacts", "TEXT NOT NULL DEFAULT '[]'")


MIGRATIONS = MIGRATIONS + ((17, _migration_17),)


def _migration_18(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS mcp_servers (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
      enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
      transport TEXT NOT NULL CHECK(transport='streamable-http'), endpoint TEXT NOT NULL,
      timeout REAL NOT NULL DEFAULT 15, status TEXT NOT NULL DEFAULT 'disconnected',
      error_category TEXT, last_connected_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS mcp_tools (
      id TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
      remote_name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', input_schema TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)), discovered_at TEXT NOT NULL,
      UNIQUE(server_id, remote_name)
    );
    """)


MIGRATIONS = MIGRATIONS + ((18, _migration_18),)


def _migration_19(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "mcp_servers", "auth_type", "TEXT NOT NULL DEFAULT 'none' CHECK(auth_type IN ('none','bearer'))")
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS mcp_server_credentials (
      server_id TEXT PRIMARY KEY REFERENCES mcp_servers(id) ON DELETE CASCADE,
      bearer_token TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS agent_runtime_settings (
      id INTEGER PRIMARY KEY CHECK(id=1),
      max_tool_calls INTEGER NOT NULL DEFAULT 10 CHECK(max_tool_calls BETWEEN 1 AND 50),
      updated_at TEXT NOT NULL
    );
    INSERT OR IGNORE INTO agent_runtime_settings(id,max_tool_calls,updated_at) VALUES(1,10,datetime('now'));
    """)


MIGRATIONS = MIGRATIONS + ((19, _migration_19),)


def _migration_20(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS aemet_credentials (id INTEGER PRIMARY KEY CHECK(id=1), api_key TEXT NOT NULL, updated_at TEXT NOT NULL)")


MIGRATIONS = MIGRATIONS + ((20, _migration_20),)


def _migration_21(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "mcp_tools", "action", "TEXT NOT NULL DEFAULT 'unknown'")


MIGRATIONS = MIGRATIONS + ((21, _migration_21),)


def _migration_22(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "agent_profiles", "max_tool_calls", "INTEGER NOT NULL DEFAULT 10 CHECK(max_tool_calls BETWEEN 1 AND 50)")
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS agent_profile_notebooks (
      agent_profile_id TEXT NOT NULL REFERENCES agent_profiles(id) ON DELETE CASCADE,
      notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
      PRIMARY KEY(agent_profile_id, notebook_id)
    );
    CREATE INDEX IF NOT EXISTS idx_agent_profile_notebooks_notebook ON agent_profile_notebooks(notebook_id);
    """)


MIGRATIONS = MIGRATIONS + ((22, _migration_22),)


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    for version, migration in MIGRATIONS:
        if version in applied:
            continue
        migration(connection)
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, datetime('now'))", (version,))
