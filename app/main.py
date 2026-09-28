from __future__ import annotations

import json
import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import AgentRunRequest, AgentRuntime, AgentRuntimeLimits, EffectiveRunConfiguration
from app.agent_model import OpenAICompatibleModelAdapter
from app.capabilities import normalize_model_capabilities
from app.diagnostics import diagnostic, recent
from app.decision.models import ShadowDecision
from app.kernel import ModuleRegistry, ToolExecutionContext, enabled_module_ids
from app.modules.attachments import AttachmentsModule
from app.modules.mcp import MCPModule
from app.modules.web_search_searxng import WebSearchSearxngModule
from app.modules.decision_runtime import DecisionRuntimeModule
from app.migrations import migrate
from app.tools import ExposurePolicy, ToolExecutor
from app.runtime_trace import RuntimeEventSink, safe_metadata
from app.agent_profiles import AgentProfileInput, AgentProfileRepository, AgentProfileResolver, AgentProfileService, ProfileNotFoundError, ProfileResolutionError, ProfileValidationError
from app.notebooks import NotebookInput, NotebookNotFoundError, NotebookRepository, NotebookService, NotebookSourceNotFoundError, NotebookValidationError
from app.ingestion import NotebookIngestionService
from app.embeddings import EmbeddingError, OpenAICompatibleEmbeddingProvider
from app.retrieval import RetrievalError, RetrievalService
from app.vector_index import SQLiteVectorIndex
from app.grounding import GroundedContext, cited_results
from app.knowledge import EmbeddingConfiguration, KnowledgeConfigurationError, bootstrap_values, validate_configuration
from app.chunking import ChunkingConfig

DATA_DIR = Path(os.getenv("NEXO_DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "nexo.sqlite3"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_UPLOAD = int(os.getenv("NEXO_MAX_UPLOAD_MB", "15")) * 1024 * 1024
app = FastAPI(title="Nexo Chat", version="0.1.0")
logger = logging.getLogger("nexo.chat")
module_registry = ModuleRegistry(app, {"max_upload": MAX_UPLOAD})
agent_runtime = AgentRuntime(AgentRuntimeLimits(max_tool_rounds=3, max_tool_output_chars=int(os.getenv("NEXO_MAX_TOOL_OUTPUT_CHARS", "12000"))))
exposure_policy = ExposurePolicy()
shadow_tasks: set[asyncio.Task[None]] = set()
enabled_modules = enabled_module_ids()
if "attachments" in enabled_modules:
    module_registry.register(AttachmentsModule())
if "web-search-searxng" in enabled_modules:
    module_registry.context.settings.update({
        "searxng_url": os.getenv("NEXO_SEARXNG_URL", ""),
        "searxng_language": os.getenv("NEXO_SEARXNG_LANGUAGE", "all"),
        "searxng_safesearch": os.getenv("NEXO_SEARXNG_SAFESEARCH", "1"),
        "searxng_max_results": os.getenv("NEXO_SEARXNG_MAX_RESULTS", "5"),
        "searxng_timeout": os.getenv("NEXO_SEARXNG_TIMEOUT", "10"),
    })
    module_registry.register(WebSearchSearxngModule())
if "mcp" in enabled_modules:
    module_registry.context.settings["mcp_servers"] = os.getenv("NEXO_MCP_SERVERS", "[]")
    module_registry.register(MCPModule())
if "decision-runtime" in enabled_modules:
    module_registry.context.settings.update({
        "decision_provider": os.getenv("NEXO_DECISION_PROVIDER", "arbiter"),
        "decision_timeout": os.getenv("NEXO_DECISION_TIMEOUT", "10"),
        "decision_model": os.getenv("NEXO_DECISION_MODEL", "jev-latest"),
        "decision_shadow": os.getenv("NEXO_DECISION_SHADOW", "false"),
        "arbiter_url": os.getenv("NEXO_ARBITER_URL", ""),
        "arbiter_api_key": os.getenv("NEXO_ARBITER_API_KEY", ""),
    })
    module_registry.register(DecisionRuntimeModule())


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


agent_profiles = AgentProfileService(AgentProfileRepository(db, now), module_registry.tool_catalog)
agent_profile_resolver = AgentProfileResolver(agent_profiles.repository)
notebooks = NotebookService(NotebookRepository(db, now), DATA_DIR / "notebook-sources")
ingestion = NotebookIngestionService(notebooks.repository, notebooks.storage_root, now, MAX_UPLOAD)


def retrieval_service(client: httpx.AsyncClient) -> RetrievalService:
    configuration = embedding_configuration()
    if configuration:
        with db() as connection:
            provider_row = connection.execute("SELECT * FROM providers WHERE id=?", (configuration.provider_id,)).fetchone()
        if not provider_row:
            raise RetrievalError("embedding provider is not configured")
        base_url, model_id, api_key = provider_row["base_url"], configuration.model_id, provider_row["api_key"]
        headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {api_key}"} if api_key else {})
        provider = OpenAICompatibleEmbeddingProvider(client, base_url, headers, model_id)
        return RetrievalService(notebooks.repository, SQLiteVectorIndex(db, now), provider,
                                ChunkingConfig(configuration.target_chunk_size, configuration.max_chunk_size, configuration.overlap),
                                configuration.batch_size, configuration, configuration.provider_id, configuration.model_id)
    base_url = os.getenv("NEXO_EMBEDDING_BASE_URL", "").strip()
    if not base_url:
        raise RetrievalError("embedding provider is not configured")
    headers = {"Content-Type": "application/json"}
    if os.getenv("NEXO_EMBEDDING_API_KEY", ""):
        headers["Authorization"] = f"Bearer {os.getenv('NEXO_EMBEDDING_API_KEY')}"
    model_id = os.getenv("NEXO_EMBEDDING_MODEL", "embedding-model")
    provider = OpenAICompatibleEmbeddingProvider(client, base_url, headers, model_id)
    return RetrievalService(notebooks.repository, SQLiteVectorIndex(db, now), provider, batch_size=int(os.getenv("NEXO_EMBEDDING_BATCH_SIZE", "32")))


@app.on_event("startup")
def startup() -> None:
    with db() as c:
        migrate(c)
    module_registry.startup()
    try:
        asyncio.get_running_loop().create_task(_refresh_all_provider_capabilities())
    except RuntimeError:
        pass


@app.on_event("shutdown")
def shutdown() -> None:
    module_registry.shutdown()


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = ""
    models: list[str] = []
    model_capabilities: dict[str, list[str]] = {}


class ChatIn(BaseModel):
    conversation_id: str | None = None
    provider_id: str
    model_id: str
    content: str
    attachments: list[dict[str, Any]] = []
    temperature: float | None = None
    agent_profile_id: str | None = None
    notebook_id: str | None = None
    execution_mode: Literal["chat", "agent"] | None = None
    web_enabled: bool | None = None
    tools_enabled: bool | None = None


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    agent_profile_id: str | None = None
    notebook_id: str | None = None
    execution_mode: Literal["chat", "agent"] | None = None


class AgentProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    system_instructions: str = Field(default="", max_length=20000)
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    tool_names: list[str] = Field(default_factory=list)


class AgentProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    provider_id: str | None = None
    model_id: str | None = None
    system_instructions: str | None = Field(default=None, max_length=20000)
    model_parameters: dict[str, Any] | None = None
    enabled: bool | None = None
    tool_names: list[str] | None = None


class NotebookIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class NotebookPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class RetrievalIn(BaseModel):
    query: str = Field(min_length=1, max_length=10000)
    limit: int = Field(default=5, ge=1, le=50)


class EmbeddingConfigurationIn(BaseModel):
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    target_chunk_size: int = Field(default=400, ge=1, le=10000)
    max_chunk_size: int = Field(default=600, ge=1, le=20000)
    overlap: int = Field(default=40, ge=0, le=5000)
    batch_size: int = Field(default=32, ge=1, le=256)
    retrieval_top_k: int = Field(default=5, ge=1, le=50)
    retrieval_max_context_chars: int = Field(default=12000, ge=1000, le=1000000)


def embedding_configuration() -> EmbeddingConfiguration | None:
    with db() as connection:
        migrate(connection)
        row = connection.execute("SELECT * FROM embedding_configurations ORDER BY config_version DESC LIMIT 1").fetchone()
        if row:
            return EmbeddingConfiguration(**dict(row))
        values = bootstrap_values()
        provider_id = values["provider_id"]
        if not provider_id:
            provider = connection.execute("SELECT id FROM providers WHERE base_url=? ORDER BY created_at LIMIT 1", (os.getenv("NEXO_EMBEDDING_BASE_URL", "").rstrip("/"),)).fetchone()
            provider_id = provider["id"] if provider else ""
        if not provider_id:
            return None
        values["provider_id"] = provider_id
        try:
            values = validate_configuration(values)
        except KnowledgeConfigurationError:
            return None
        timestamp = now()
        connection.execute("""INSERT INTO embedding_configurations
            (id,provider_id,model_id,target_chunk_size,max_chunk_size,overlap,batch_size,retrieval_top_k,retrieval_max_context_chars,config_version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (str(uuid.uuid4()), values["provider_id"], values["model_id"], values["target_chunk_size"], values["max_chunk_size"], values["overlap"], values["batch_size"], values["retrieval_top_k"], values["retrieval_max_context_chars"], 1, timestamp, timestamp))
        return EmbeddingConfiguration(**dict(connection.execute("SELECT * FROM embedding_configurations ORDER BY config_version DESC LIMIT 1").fetchone()))


def knowledge_health() -> dict[str, int]:
    with db() as connection:
        migrate(connection)
        rows = connection.execute("SELECT indexing_status, COUNT(*) AS count FROM notebook_sources GROUP BY indexing_status").fetchall()
        totals = connection.execute("SELECT COUNT(*) AS documents, COALESCE(SUM(chunk_count),0) AS chunks, COALESCE(SUM(vector_count),0) AS vectors FROM notebook_sources WHERE status='ready'").fetchone()
    counts = {row["indexing_status"]: row["count"] for row in rows}
    return {"ready": counts.get("ready", 0), "outdated": counts.get("outdated", 0), "failed": counts.get("failed", 0), "not_indexed": counts.get("not_indexed", 0), "legacy": counts.get("legacy", 0), "documents": totals["documents"], "chunks": totals["chunks"], "vectors": totals["vectors"]}


def profile_input(item: AgentProfileIn) -> AgentProfileInput:
    return AgentProfileInput(item.name, item.description, item.provider_id, item.model_id, item.system_instructions,
                             item.model_parameters, item.enabled, tuple(item.tool_names))


def profile_error(error: Exception) -> HTTPException:
    return HTTPException(404 if isinstance(error, ProfileNotFoundError) else 400, str(error))


def provider_dict(row: sqlite3.Row) -> dict[str, Any]:
    with db() as c:
        models = c.execute("SELECT id,label,capabilities FROM models WHERE provider_id=? ORDER BY label", (row["id"],)).fetchall()
        refreshed = c.execute("SELECT refreshed_at FROM capability_refreshes WHERE provider_id=?", (row["id"],)).fetchone()
    return {"id": row["id"], "name": row["name"], "base_url": row["base_url"],
            "has_api_key": bool(row["api_key"]), "capabilities_refreshed_at": refreshed["refreshed_at"] if refreshed else None, "models": [{**dict(m), "capabilities": json.loads(m["capabilities"])} for m in models]}


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version}


@app.get("/api/settings/embeddings")
def get_embedding_settings():
    configuration = embedding_configuration()
    with db() as connection:
        providers_rows = connection.execute("SELECT * FROM providers ORDER BY name").fetchall()
    health = knowledge_health()
    return {"configuration": configuration.public() if configuration else None, "providers": [provider_dict(row) for row in providers_rows], "health": health}


@app.put("/api/settings/embeddings")
def save_embedding_settings(item: EmbeddingConfigurationIn):
    try:
        values = validate_configuration(item.model_dump())
    except KnowledgeConfigurationError as error:
        raise HTTPException(400, str(error))
    with db() as connection:
        provider = connection.execute("SELECT id FROM providers WHERE id=?", (values["provider_id"],)).fetchone()
        model = connection.execute("SELECT capabilities FROM models WHERE provider_id=? AND id=?", (values["provider_id"], values["model_id"])).fetchone()
        if not provider or not model:
            raise HTTPException(400, "provider_id and model_id must reference an existing provider model")
        if "embedding" not in json.loads(model["capabilities"] or "[]"):
            raise HTTPException(400, "model must be explicitly designated with embedding capability")
        old = connection.execute("SELECT * FROM embedding_configurations ORDER BY config_version DESC LIMIT 1").fetchone()
        version = (old["config_version"] + 1) if old else 1
        timestamp = now()
        connection.execute("""INSERT INTO embedding_configurations
            (id,provider_id,model_id,target_chunk_size,max_chunk_size,overlap,batch_size,retrieval_top_k,retrieval_max_context_chars,config_version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (str(uuid.uuid4()), values["provider_id"], values["model_id"], values["target_chunk_size"], values["max_chunk_size"], values["overlap"], values["batch_size"], values["retrieval_top_k"], values["retrieval_max_context_chars"], version, old["created_at"] if old else timestamp, timestamp))
        affecting = not old or any(old[key] != values[key] for key in ("provider_id", "model_id", "target_chunk_size", "max_chunk_size", "overlap"))
        if affecting and old:
            connection.execute("UPDATE notebook_sources SET indexing_status='outdated', indexing_error=NULL, updated_at=? WHERE indexing_status='ready'", (timestamp,))
        row = connection.execute("SELECT * FROM embedding_configurations WHERE config_version=?", (version,)).fetchone()
    return {"configuration": EmbeddingConfiguration(**dict(row)).public(), "invalidated": affecting, "outdated_sources": knowledge_health()["outdated"]}


@app.post("/api/settings/embeddings/test")
async def test_embedding():
    configuration = embedding_configuration()
    if not configuration:
        raise HTTPException(400, "embedding configuration is not available")
    started = asyncio.get_running_loop().time()
    try:
        async with httpx.AsyncClient(timeout=float(os.getenv("NEXO_EMBEDDING_TIMEOUT", "30"))) as client:
            service = retrieval_service(client)
            batch = await service.provider.embed(["Nexo embedding connectivity test"])
        dimension = len(batch.vectors[0]) if batch.vectors else 0
        return {"ok": True, "provider": configuration.provider_id, "model": configuration.model_id, "dimension": dimension, "latency_ms": round((asyncio.get_running_loop().time() - started) * 1000, 2)}
    except Exception as error:
        raise HTTPException(502, f"Embedding test failed: {str(error)[:180]}")


@app.patch("/api/providers/{pid}/models/{model_id:path}")
def update_model_capability(pid: str, model_id: str, item: dict[str, Any]):
    capabilities = item.get("capabilities")
    if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
        raise HTTPException(400, "capabilities must be a list of strings")
    with db() as connection:
        if not connection.execute("SELECT 1 FROM models WHERE provider_id=? AND id=?", (pid, model_id)).fetchone():
            raise HTTPException(404, "Model not found")
        connection.execute("UPDATE models SET capabilities=? WHERE provider_id=? AND id=?", (json.dumps(sorted(set(capabilities))), pid, model_id))
        provider = connection.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    return provider_dict(provider)


@app.get("/api/modules")
def modules():
    return module_registry.catalog()


@app.get("/api/tools")
def tools():
    """Return the registered tools without exposing executable handlers."""
    return module_registry.tool_catalog()


@app.get("/api/agents")
def list_agents():
    return agent_profiles.list()


@app.post("/api/agents")
def create_agent(item: AgentProfileIn):
    try:
        return agent_profiles.create(profile_input(item))
    except (ProfileNotFoundError, ProfileValidationError) as error:
        raise profile_error(error)


@app.get("/api/agents/{profile_id}")
def get_agent(profile_id: str):
    try:
        return agent_profiles.get(profile_id)
    except ProfileNotFoundError as error:
        raise profile_error(error)


@app.patch("/api/agents/{profile_id}")
def update_agent(profile_id: str, item: AgentProfilePatch):
    try:
        return agent_profiles.update(profile_id, item.model_dump(exclude_unset=True))
    except (ProfileNotFoundError, ProfileValidationError) as error:
        raise profile_error(error)


@app.delete("/api/agents/{profile_id}")
def delete_agent(profile_id: str):
    try:
        agent_profiles.delete(profile_id)
    except ProfileNotFoundError as error:
        raise profile_error(error)
    return {"ok": True}


def notebook_error(error: Exception) -> HTTPException:
    if isinstance(error, (NotebookNotFoundError, NotebookSourceNotFoundError)):
        return HTTPException(404, str(error))
    return HTTPException(400, str(error))


@app.get("/api/notebooks")
def list_notebooks():
    return notebooks.list()


@app.post("/api/notebooks")
def create_notebook(item: NotebookIn):
    try:
        return notebooks.create(NotebookInput(item.name, item.description))
    except NotebookValidationError as error:
        raise notebook_error(error)


@app.get("/api/notebooks/{notebook_id}")
def get_notebook(notebook_id: str):
    try:
        return notebooks.get(notebook_id)
    except NotebookNotFoundError as error:
        raise notebook_error(error)


@app.patch("/api/notebooks/{notebook_id}")
def update_notebook(notebook_id: str, item: NotebookPatch):
    try:
        return notebooks.update(notebook_id, item.model_dump(exclude_unset=True))
    except (NotebookNotFoundError, NotebookValidationError) as error:
        raise notebook_error(error)


@app.delete("/api/notebooks/{notebook_id}")
def delete_notebook(notebook_id: str):
    try:
        notebooks.delete(notebook_id)
    except NotebookNotFoundError as error:
        raise notebook_error(error)
    return {"ok": True}


@app.get("/api/notebooks/{notebook_id}/sources")
def list_notebook_sources(notebook_id: str):
    try:
        return notebooks.sources(notebook_id)
    except NotebookNotFoundError as error:
        raise notebook_error(error)


@app.get("/api/notebooks/{notebook_id}/sources/{source_id}")
def get_notebook_source(notebook_id: str, source_id: str):
    try:
        return notebooks.source(notebook_id, source_id)
    except (NotebookNotFoundError, NotebookSourceNotFoundError) as error:
        raise notebook_error(error)


@app.get("/api/notebooks/{notebook_id}/sources/{source_id}/document")
def get_canonical_document(notebook_id: str, source_id: str):
    try:
        document = notebooks.canonical(notebook_id, source_id)
        return document or {"status": "not_ready", "document": None}
    except (NotebookNotFoundError, NotebookSourceNotFoundError) as error:
        raise notebook_error(error)


@app.get("/api/notebooks/{notebook_id}/sources/{source_id}/index")
def get_source_index(notebook_id: str, source_id: str):
    try:
        source = notebooks.source(notebook_id, source_id)
        document = notebooks.canonical(notebook_id, source_id)
        with db() as connection:
            identity = connection.execute("SELECT * FROM vector_index_identities WHERE document_id=?", (document["id"],)).fetchone() if document else None
            vectors = connection.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=? AND embedding IS NOT NULL", (document["id"],)).fetchone()[0] if document else 0
        result = dict(source)
        result["indexing_identity"] = dict(identity) if identity else None
        result["vector_count_actual"] = vectors
        configuration = embedding_configuration()
        compatible = bool(identity and configuration and identity["provider_id"] == configuration.provider_id
                         and identity["model_id"] == configuration.model_id
                         and identity["embedding_config_version"] == configuration.config_version
                         and identity["chunking_config_hash"] == configuration.chunking_hash()
                         and document and identity["document_hash"] == document["content_hash"])
        if source["indexing_status"] == "ready" and not identity:
            result["indexing_status"] = "legacy"
        elif source["indexing_status"] == "ready" and not compatible:
            result["indexing_status"] = "outdated"
        result["compatibility_status"] = "current" if compatible else result["indexing_status"]
        return result
    except (NotebookNotFoundError, NotebookSourceNotFoundError) as error:
        raise notebook_error(error)


@app.get("/api/notebooks/{notebook_id}/sources/{source_id}/integrity")
def source_integrity(notebook_id: str, source_id: str):
    try:
        notebooks.source(notebook_id, source_id)
        configuration = embedding_configuration()
        service = RetrievalService(notebooks.repository, SQLiteVectorIndex(db, now), object(),
                                   ChunkingConfig(configuration.target_chunk_size, configuration.max_chunk_size, configuration.overlap) if configuration else None,
                                   configuration.batch_size if configuration else 32, configuration,
                                   configuration.provider_id if configuration else None, configuration.model_id if configuration else None)
        return service.integrity(notebook_id, source_id)
    except (NotebookNotFoundError, NotebookSourceNotFoundError) as error:
        raise notebook_error(error)


@app.post("/api/notebooks/{notebook_id}/sources/{source_id}/ingest")
def ingest_notebook_source(notebook_id: str, source_id: str):
    try:
        notebooks.source(notebook_id, source_id)
        result = ingestion.ingest(notebook_id, source_id)
        return {"source": notebooks.source(notebook_id, source_id), "document": result}
    except (NotebookNotFoundError, NotebookSourceNotFoundError) as error:
        raise notebook_error(error)


@app.post("/api/notebooks/{notebook_id}/sources/{source_id}/index")
async def index_notebook_source(notebook_id: str, source_id: str):
    try:
        notebooks.source(notebook_id, source_id)
        async with httpx.AsyncClient(timeout=float(os.getenv("NEXO_EMBEDDING_TIMEOUT", "30"))) as client:
            service = retrieval_service(client)
            result = await service.index_source(notebook_id, source_id)
        return {"source": notebooks.source(notebook_id, source_id), "index": result}
    except (NotebookNotFoundError, NotebookSourceNotFoundError) as error:
        raise notebook_error(error)
    except (RetrievalError, EmbeddingError) as error:
        raise HTTPException(400, str(error))


@app.post("/api/notebooks/{notebook_id}/retrieve")
async def retrieve_notebook(notebook_id: str, item: RetrievalIn):
    try:
        notebooks.get(notebook_id)
        async with httpx.AsyncClient(timeout=float(os.getenv("NEXO_EMBEDDING_TIMEOUT", "30"))) as client:
            return {"results": await retrieval_service(client).search(notebook_id, item.query, item.limit)}
    except NotebookNotFoundError as error:
        raise notebook_error(error)
    except (RetrievalError, EmbeddingError) as error:
        raise HTTPException(400, str(error))


@app.post("/api/notebooks/{notebook_id}/retrieve/diagnostics")
async def inspect_notebook_retrieval(notebook_id: str, item: RetrievalIn):
    """Return ranked candidate metadata without document text or vector values."""
    try:
        notebooks.get(notebook_id)
        async with httpx.AsyncClient(timeout=float(os.getenv("NEXO_EMBEDDING_TIMEOUT", "30"))) as client:
            candidates = await retrieval_service(client).inspect_search(notebook_id, item.query, item.limit)
        return {"candidate_count": len(candidates), "accepted_count": sum(item["accepted"] for item in candidates),
                "rejected_count": sum(not item["accepted"] for item in candidates), "candidates": candidates}
    except NotebookNotFoundError as error:
        raise notebook_error(error)
    except (RetrievalError, EmbeddingError) as error:
        raise HTTPException(400, str(error))


@app.post("/api/notebooks/{notebook_id}/sources")
async def add_notebook_source(notebook_id: str, request: Request):
    try:
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            form = await request.form()
            file = form.get("file")
            if file is None or not hasattr(file, "filename") or not hasattr(file, "read"):
                raise NotebookValidationError("file is required")
            data = await file.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "File exceeds upload limit")
            return notebooks.add_file(notebook_id, str(form.get("title") or file.filename or ""), file.filename or "", file.content_type or "application/octet-stream", data)
        payload = await request.json()
        source_type = payload.get("type")
        if source_type != "web":
            raise NotebookValidationError("JSON sources must have type web")
        return notebooks.add_web(notebook_id, payload.get("title", ""), payload.get("url", ""), payload.get("metadata"))
    except HTTPException:
        raise
    except (NotebookNotFoundError, NotebookValidationError) as error:
        raise notebook_error(error)


@app.delete("/api/notebooks/{notebook_id}/sources/{source_id}")
def delete_notebook_source(notebook_id: str, source_id: str):
    try:
        notebooks.delete_source(notebook_id, source_id)
    except (NotebookNotFoundError, NotebookSourceNotFoundError) as error:
        raise notebook_error(error)
    return {"ok": True}


@app.get("/api/lab/shadow")
def shadow_observations(conversation_id: str | None = None, limit: int = 20):
    limit = min(max(limit, 1), 100)
    query = "SELECT * FROM shadow_observations"
    params: list[Any] = []
    if conversation_id:
        query += " WHERE conversation_id=?"
        params.append(conversation_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with db() as c:
        rows = c.execute(query, params).fetchall()
    return [{**dict(row), "answers": json.loads(row["answers"]), "execution": json.loads(row["execution"]), "metadata": json.loads(row["metadata"])} for row in rows]


@app.get("/api/lab/diagnostics")
def diagnostics(limit: int = 50):
    return recent(limit)


@app.get("/api/lab/traces")
def traces(conversation_id: str | None = None, message_id: str | None = None, run_id: str | None = None, limit: int = 20):
    limit = min(max(limit, 1), 100)
    query = """SELECT e.id, r.conversation_id, r.message_id, e.sequence, e.kind AS type,
        e.started_at, e.completed_at, e.duration_ms, e.status, e.safe_metadata AS metadata,
        e.run_id FROM runtime_events e JOIN runtime_runs r ON r.id=e.run_id"""
    params: list[Any] = []
    if conversation_id:
        query += " WHERE r.conversation_id=?"
        params.append(conversation_id)
    if message_id:
        query += " AND " if conversation_id else " WHERE "
        query += "r.message_id=?"
        params.append(message_id)
    if run_id:
        query += " AND " if conversation_id or message_id else " WHERE "
        query += "e.run_id=?"
        params.append(run_id)
    query += " ORDER BY e.started_at DESC, e.sequence DESC LIMIT ?"
    params.append(limit)
    with db() as c:
        rows = c.execute(query, params).fetchall()
        if not rows:
            legacy = "SELECT * FROM runtime_trace_events"
            legacy_params: list[Any] = []
            legacy_filters = []
            if conversation_id:
                legacy_filters.append("conversation_id=?"); legacy_params.append(conversation_id)
            if message_id:
                legacy_filters.append("message_id=?"); legacy_params.append(message_id)
            if legacy_filters: legacy += " WHERE " + " AND ".join(legacy_filters)
            legacy += " ORDER BY started_at DESC, sequence DESC LIMIT ?"
            legacy_params.append(limit)
            rows = c.execute(legacy, legacy_params).fetchall()
    return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]


@app.get("/api/lab/runs")
def runtime_runs(conversation_id: str | None = None, message_id: str | None = None, run_id: str | None = None, limit: int = 20):
    limit = min(max(limit, 1), 100)
    query = "SELECT * FROM runtime_runs"
    params: list[Any] = []
    filters = []
    if conversation_id:
        filters.append("conversation_id=?"); params.append(conversation_id)
    if message_id:
        filters.append("message_id=?"); params.append(message_id)
    if run_id:
        filters.append("id=?"); params.append(run_id)
    if filters: query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY started_at DESC LIMIT ?"; params.append(limit)
    with db() as c: rows = c.execute(query, params).fetchall()
    return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]


@app.get("/api/lab/runs/{run_id}")
def runtime_run(run_id: str):
    with db() as c:
        run = c.execute("SELECT * FROM runtime_runs WHERE id=?", (run_id,)).fetchone()
        if not run: raise HTTPException(404, "Runtime run not found")
        events = c.execute("SELECT * FROM runtime_events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
    return {"run": {**dict(run), "metadata": json.loads(run["metadata"])}, "events": [{**dict(event), "safe_metadata": json.loads(event["safe_metadata"])} for event in events]}


@app.get("/api/lab/runs/{run_id}/events")
def runtime_run_events(run_id: str):
    return runtime_run(run_id)["events"]


@app.get("/api/lab/traces/{message_id}")
def message_trace(message_id: str):
    with db() as c:
        rows = c.execute("""SELECT e.id, r.conversation_id, r.message_id, e.sequence, e.kind AS type,
            e.started_at, e.completed_at, e.duration_ms, e.status, e.safe_metadata AS metadata, e.run_id
            FROM runtime_events e JOIN runtime_runs r ON r.id=e.run_id WHERE r.message_id=? ORDER BY e.sequence""", (message_id,)).fetchall()
        if not rows:
            rows = c.execute("SELECT * FROM runtime_trace_events WHERE message_id=? ORDER BY sequence", (message_id,)).fetchall()
    return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]


@app.get("/api/providers")
def providers():
    with db() as c:
        rows = c.execute("SELECT * FROM providers ORDER BY name").fetchall()
    return [provider_dict(r) for r in rows]


@app.post("/api/providers")
def add_provider(item: ProviderIn):
    pid = str(uuid.uuid4())
    url = item.base_url.strip().rstrip("/")
    with db() as c:
        c.execute("INSERT INTO providers(id,name,base_url,api_key,created_at) VALUES(?,?,?,?,?)", (pid, item.name.strip(), url, item.api_key, now()))
        c.executemany("INSERT OR IGNORE INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?)", [(m.strip(), pid, m.strip(), json.dumps(item.model_capabilities.get(m.strip(), []))) for m in item.models if m.strip()])
        row = c.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    return provider_dict(row)


@app.put("/api/providers/{pid}")
def update_provider(pid: str, item: ProviderIn):
    with db() as c:
        old = c.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
        if not old: raise HTTPException(404, "Provider not found")
        key = item.api_key if item.api_key else old["api_key"]
        c.execute("UPDATE providers SET name=?,base_url=?,api_key=? WHERE id=?", (item.name.strip(), item.base_url.strip().rstrip("/"), key, pid))
        for m in item.models:
            if m.strip(): c.execute("INSERT OR IGNORE INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?)", (m.strip(), pid, m.strip(), json.dumps(item.model_capabilities.get(m.strip(), []))))
        row = c.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    return provider_dict(row)


@app.delete("/api/providers/{pid}")
def delete_provider(pid: str):
    with db() as c: c.execute("DELETE FROM providers WHERE id=?", (pid,))
    return {"ok": True}


async def provider_request(provider_id: str, path: str, **kwargs):
    with db() as c: p = c.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    if not p: raise HTTPException(404, "Provider not found")
    headers = {"Authorization": f"Bearer {p['api_key']}"} if p["api_key"] else {}
    url = p["base_url"].rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Provider returned HTTP {e.response.status_code}")
    except (httpx.RequestError, ValueError) as e:
        raise HTTPException(502, f"Could not reach provider: {str(e)[:180]}")


async def _refresh_all_provider_capabilities() -> None:
    with db() as c:
        provider_ids = [row["id"] for row in c.execute("SELECT id FROM providers")]
    for provider_id in provider_ids:
        try:
            await refresh_models(provider_id)
        except Exception as exc:
            diagnostic(logger, "capability_refresh_failed", provider_id=provider_id, error_code=type(exc).__name__)


@app.post("/api/providers/{pid}/refresh")
async def refresh_models(pid: str):
    payload = await provider_request(pid, "/models")
    data = payload.get("data", [])
    models = [m for m in data if isinstance(m, dict) and m.get("id")]
    with db() as c:
        for model in models:
            capabilities = normalize_model_capabilities(model, _tool_calling_fallback())
            diagnostic(logger, "model_capabilities", **{
                "model": str(model["id"]),
                "raw_capabilities": model.get("capabilities"),
                "raw_supports_tools": model.get("supports_tools"),
                "raw_tool_calling": model.get("tool_calling"),
                "raw_supported_parameters": model.get("supported_parameters"),
                "normalized_capabilities": sorted(capabilities),
            })
            c.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?) ON CONFLICT(provider_id,id) DO UPDATE SET capabilities=excluded.capabilities", (str(model["id"]), pid, str(model["id"]), json.dumps(sorted(capabilities))))
        c.execute("INSERT INTO capability_refreshes(provider_id,refreshed_at) VALUES(?,?) ON CONFLICT(provider_id) DO UPDATE SET refreshed_at=excluded.refreshed_at", (pid, now()))
        row = c.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    return provider_dict(row)


@app.delete("/api/providers/{pid}/models/{model_id:path}")
def delete_model(pid: str, model_id: str):
    with db() as c: c.execute("DELETE FROM models WHERE provider_id=? AND id=?", (pid, model_id))
    return {"ok": True}


@app.get("/api/conversations")
def conversations():
    with db() as c: rows = c.execute("SELECT id,title,created_at,updated_at,execution_mode,agent_profile_id,notebook_id FROM conversations ORDER BY updated_at DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/conversations/{cid}")
def get_conversation(cid: str):
    with db() as c:
        conv = c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if not conv: raise HTTPException(404, "Conversation not found")
        msgs = c.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (cid,)).fetchall()
    citation_map: dict[str, list[dict[str, Any]]] = {}
    with db() as c:
        all_citations = c.execute("""SELECT mc.*, ns.title AS source_title,
            cd.content_hash AS current_document_content_hash, dc.content_hash AS current_chunk_content_hash
            FROM message_citations mc
            LEFT JOIN notebook_sources ns ON ns.id=mc.source_id
            LEFT JOIN canonical_documents cd ON cd.id=mc.document_id
            LEFT JOIN document_chunks dc ON dc.id=mc.chunk_id
            WHERE mc.message_id IN (SELECT id FROM messages WHERE conversation_id=?)
            ORDER BY mc.message_id, mc.citation_key""", (cid,)).fetchall()
    for citation in all_citations:
        item = dict(citation)
        item["provenance"] = json.loads(item["provenance"] or "[]")
        if not citation["current_document_content_hash"] or not citation["current_chunk_content_hash"]:
            item["status"] = "unavailable"
        elif citation["current_document_content_hash"] != citation["document_content_hash"] or citation["current_chunk_content_hash"] != citation["chunk_content_hash"]:
            item["status"] = "changed"
        else:
            item["status"] = "valid"
        citation_map.setdefault(citation["message_id"], []).append(item)
    return {"conversation": dict(conv), "messages": [{**dict(m), "attachments": json.loads(m["attachments"]), "sources": json.loads(m["sources"]), "runtime": json.loads(m["runtime_metadata"] or "{}"), "citations": citation_map.get(m["id"], [])} for m in msgs]}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str):
    with db() as c: c.execute("DELETE FROM conversations WHERE id=?", (cid,))
    return {"ok": True}


@app.patch("/api/conversations/{cid}")
def update_conversation(cid: str, item: ConversationPatch):
    with db() as c:
        if c.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone() is None:
            raise HTTPException(404, "Conversation not found")
        if item.agent_profile_id is not None and c.execute("SELECT 1 FROM agent_profiles WHERE id=?", (item.agent_profile_id,)).fetchone() is None:
            raise HTTPException(404, "agent_profile_not_found")
        if item.notebook_id is not None and c.execute("SELECT 1 FROM notebooks WHERE id=?", (item.notebook_id,)).fetchone() is None:
            raise HTTPException(404, "notebook_not_found")
        current = c.execute("SELECT execution_mode,agent_profile_id FROM conversations WHERE id=?", (cid,)).fetchone()
        mode = item.execution_mode if "execution_mode" in item.model_fields_set else ("agent" if item.agent_profile_id is not None else current["execution_mode"])
        profile_id = item.agent_profile_id if "agent_profile_id" in item.model_fields_set else current["agent_profile_id"]
        if mode == "agent" and not profile_id:
            raise HTTPException(400, "agent_profile_required")
        if mode == "chat":
            profile_id = None
        values = ({"title": item.title.strip()} if "title" in item.model_fields_set and item.title else {})
        values.update({"execution_mode": mode} if "execution_mode" in item.model_fields_set or mode == "chat" else {})
        values.update({"agent_profile_id": profile_id} if "agent_profile_id" in item.model_fields_set or mode == "chat" else {})
        values.update({"notebook_id": item.notebook_id} if "notebook_id" in item.model_fields_set else {})
        if values:
            c.execute(f"UPDATE conversations SET {', '.join(f'{key}=?' for key in values)},updated_at=? WHERE id=?", (*values.values(), now(), cid))
        return dict(c.execute("SELECT id,title,created_at,updated_at,execution_mode,agent_profile_id,notebook_id FROM conversations WHERE id=?", (cid,)).fetchone())


@app.post("/api/chat")
async def chat(req: ChatIn):
    if not req.content.strip() and not req.attachments: raise HTTPException(400, "Message is empty")
    profile_config = None
    grounded_context: GroundedContext | None = None
    with db() as c:
        cid = req.conversation_id or str(uuid.uuid4())
        conv = c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if req.conversation_id and not conv: raise HTTPException(404, "Conversation not found")
        # Omitted means inherit; explicit null is the normal Nexo selection.
        mode = req.execution_mode if "execution_mode" in req.model_fields_set else (conv["execution_mode"] if conv else ("agent" if req.agent_profile_id else "chat"))
        profile_id = req.agent_profile_id if "agent_profile_id" in req.model_fields_set else (conv["agent_profile_id"] if conv else None)
        if mode == "agent" and not profile_id:
            raise HTTPException(400, "agent_profile_required")
        if mode == "chat":
            profile_id = None
        notebook_id = req.notebook_id if "notebook_id" in req.model_fields_set else (conv["notebook_id"] if conv else None)
        if profile_id is not None and c.execute("SELECT 1 FROM agent_profiles WHERE id=?", (profile_id,)).fetchone() is None:
            raise HTTPException(404, "agent_profile_not_found")
        if notebook_id is not None and c.execute("SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)).fetchone() is None:
            raise HTTPException(404, "notebook_not_found")
        notebook = c.execute("SELECT name FROM notebooks WHERE id=?", (notebook_id,)).fetchone() if notebook_id else None
        knowledge_config = embedding_configuration()
        if notebook_id and knowledge_config:
            indexed_sources = c.execute("""SELECT COUNT(*) FROM notebook_sources ns JOIN canonical_documents cd ON cd.source_id=ns.id
                JOIN vector_index_identities vii ON vii.document_id=cd.id
                WHERE ns.notebook_id=? AND ns.indexing_status='ready' AND vii.provider_id=? AND vii.model_id=? AND vii.embedding_config_version=? AND vii.chunking_config_hash=? AND vii.document_hash=cd.content_hash""",
                (notebook_id, knowledge_config.provider_id, knowledge_config.model_id, knowledge_config.config_version, knowledge_config.chunking_hash())).fetchone()[0]
        else:
            indexed_sources = c.execute("SELECT COUNT(*) FROM notebook_sources WHERE notebook_id=? AND indexing_status='ready'", (notebook_id,)).fetchone()[0] if notebook_id else 0
        if not conv:
            title = req.content.strip().replace("\n", " ")[:60] or "New chat"
            c.execute("INSERT INTO conversations(id,title,created_at,updated_at,execution_mode,agent_profile_id,notebook_id) VALUES(?,?,?,?,?,?,?)", (cid, title, now(), now(), mode, profile_id, notebook_id))
        elif "agent_profile_id" in req.model_fields_set or "notebook_id" in req.model_fields_set or "execution_mode" in req.model_fields_set:
            values = {"execution_mode": mode, "agent_profile_id": profile_id, "notebook_id": notebook_id}
            fields = [key for key in values if key in req.model_fields_set]
            if "execution_mode" in req.model_fields_set and mode == "chat": fields = ["execution_mode", "agent_profile_id"] + (["notebook_id"] if "notebook_id" in req.model_fields_set else [])
            c.execute(f"UPDATE conversations SET {', '.join(f'{key}=?' for key in fields)},updated_at=? WHERE id=?", (*(values[key] for key in fields), now(), cid))
        if profile_id:
            try:
                profile_config = agent_profile_resolver.resolve(profile_id, req.temperature)
            except ProfileNotFoundError:
                raise HTTPException(404, "agent_profile_not_found")
            except ProfileResolutionError as error:
                raise HTTPException(400, error.code)
        selected_provider_id = profile_config.provider_id if profile_config else req.provider_id
        selected_model_id = profile_config.model_id if profile_config else req.model_id
        provider = c.execute("SELECT * FROM providers WHERE id=?", (selected_provider_id,)).fetchone()
        if not provider: raise HTTPException(404, "Provider not found")
        model = c.execute("SELECT * FROM models WHERE provider_id=? AND id=?", (selected_provider_id, selected_model_id)).fetchone()
        if not model:
            raise HTTPException(400, "agent_model_unavailable" if profile_config else "Choose a model configured for this provider")
        history = c.execute("SELECT role,content FROM messages WHERE conversation_id=? ORDER BY created_at", (cid,)).fetchall()
        user_content: Any = req.content
        if req.attachments:
            parts: list[dict[str, Any]] = []
            if req.content: parts.append({"type": "text", "text": req.content})
            for a in req.attachments:
                if a.get("kind") == "image" and a.get("data_url"):
                    parts.append({"type": "image_url", "image_url": {"url": a["data_url"]}})
                elif a.get("kind") == "text":
                    parts.append({"type": "text", "text": f"\n\n[Archivo: {a.get('name','document')} ]\n{a.get('text','')[:120000]}"})
            user_content = parts
        message_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        c.execute("INSERT INTO messages(id,conversation_id,role,content,provider_id,model_id,attachments,created_at) VALUES(?,?,?,?,?,?,?,?)", (message_id, cid, "user", req.content, selected_provider_id, selected_model_id, json.dumps(req.attachments), now()))
        c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
        runtime_snapshot = {
            "agent_profile_id": profile_config.profile_id if profile_config else None,
            "execution_mode": mode,
            "agent_profile_name": profile_config.profile_name if profile_config else None,
            "resolved_provider": selected_provider_id,
            "resolved_provider_name": provider["name"],
            "resolved_model": selected_model_id,
            "resolved_model_name": model["label"],
            "system_instructions_applied": bool(profile_config and profile_config.system_instructions),
            "notebook_id": notebook_id,
            "notebook_name": notebook["name"] if notebook else None,
            "knowledge_available": bool(notebook_id),
            "knowledge_retrieval_enabled": bool(notebook_id),
            "knowledge_indexed_sources": indexed_sources,
            "knowledge_embedding_model": knowledge_config.model_id if notebook_id and knowledge_config else None,
            "embedding_model": knowledge_config.model_id if notebook_id and knowledge_config else None,
            "generation_model": selected_model_id,
            "soul_applied": bool(profile_config),
            "index_status": ("current" if indexed_sources else "not_indexed") if notebook_id else "not_requested",
        }
        runtime_snapshot = {key: value for key, value in runtime_snapshot.items() if value is not None}
        c.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status,model,metadata) VALUES(?,?,?,?,?,?,?)", (run_id, cid, message_id, now(), "started", selected_model_id, json.dumps(runtime_snapshot)))
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": user_content})
        url, key = provider["base_url"].rstrip("/") + "/chat/completions", provider["api_key"]
        supports_tools = "tool-calling" in json.loads(model["capabilities"] or "[]")
        diagnostic(logger, "capability_policy", **{
            "model": selected_model_id,
            "reported_tool_capability": supports_tools,
            "fallback_enabled": _tool_calling_fallback(),
            "effective_tool_capability": supports_tools,
        })
    execution_context = ToolExecutionContext(cid, selected_provider_id, selected_model_id, 0, run_id)
    event_sink = SQLiteRuntimeEventSink(run_id)
    module_registry.run_hook("chat_before", {"conversation_id": cid, "provider_id": selected_provider_id, "model_id": selected_model_id})
    execution_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    if _shadow_configured():
        trace_id = _insert_trace_event(cid, message_id, "DECIDE", "running", {"model": None, "run_id": run_id})
        decide_event_id = event_sink.start_event("DECIDE", "decision shadow", {"round": 0})
        task = asyncio.create_task(_run_shadow_observation(message_id, cid, req.content, module_registry.tool_catalog(), execution_future, trace_id, run_id, event_sink, decide_event_id))
        shadow_tasks.add(task)
        task.add_done_callback(lambda finished: _finish_shadow_task(finished, cid, message_id))

    async def events():
        nonlocal grounded_context
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
                model_adapter = OpenAICompatibleModelAdapter(client, url, headers, selected_model_id)
                if notebook_id:
                    yield "data: " + json.dumps({"activity": {"type": "RETRIEVE", "status": "running"}}) + "\n\n"
                    retrieve_started = asyncio.get_running_loop().time()
                    retrieve_event = event_sink.start_event("RETRIEVE", "notebook retrieval", {"notebook_id": notebook_id, "retrieval_count": 0})
                    try:
                        config = embedding_configuration()
                        top_k = config.retrieval_top_k if config else min(max(int(os.getenv("NEXO_RAG_TOP_K", "5")), 1), 50)
                        max_chars = config.retrieval_max_context_chars if config else min(max(int(os.getenv("NEXO_RAG_MAX_CONTEXT_CHARS", "12000")), 1000), 100000)
                        retrieval = await retrieval_service(client).search(notebook_id, req.content, top_k)
                        grounded_context = GroundedContext.build(notebook_id, req.content, retrieval, max_chars)
                        retrieval_metadata = {"notebook_id": notebook_id, "retrieval_status": "completed",
                                               "retrieval_count": len(grounded_context.retrieval_results),
                                               "retrieval_result_count": len(grounded_context.retrieval_results),
                                               "retrieval_query_sha256": hashlib.sha256(req.content.encode()).hexdigest(),
                                               "retrieval_query_length": len(req.content),
                                               "selected_chunk_ids": [item["chunk_id"] for item in grounded_context.retrieval_results],
                                               "top_scores": [item.get("score") for item in grounded_context.retrieval_results],
                                               "grounded_context_created": True,
                                               "retrieval_duration_ms": round((asyncio.get_running_loop().time() - retrieve_started) * 1000, 2),
                                              "top_score": grounded_context.retrieval_results[0].get("score") if grounded_context.retrieval_results else None,
                                               "context_chars": grounded_context.context_chars, "grounding_context_chars": grounded_context.context_chars,
                                               "grounding_chunks": len(grounded_context.retrieval_results),
                                               "grounding_applied": bool(grounded_context.retrieval_results),
                                               "context_truncated": grounded_context.truncated,
                                               "knowledge_retrieval_applied": True}
                        event_sink.finish_event(retrieve_event, "completed", retrieval_metadata, retrieval_metadata["retrieval_duration_ms"])
                    except (RetrievalError, ValueError, EmbeddingError):
                        grounded_context = GroundedContext.build(notebook_id, req.content, [], 0)
                        retrieval_metadata = {"notebook_id": notebook_id, "retrieval_status": "failed", "retrieval_count": 0,
                                               "retrieval_result_count": 0,
                                               "retrieval_query_sha256": hashlib.sha256(req.content.encode()).hexdigest(),
                                               "retrieval_query_length": len(req.content),
                                               "selected_chunk_ids": [], "top_scores": [],
                                               "grounded_context_created": True,
                                               "retrieval_duration_ms": round((asyncio.get_running_loop().time() - retrieve_started) * 1000, 2),
                                               "context_chars": 0, "grounding_context_chars": 0, "grounding_chunks": 0, "grounding_applied": False, "context_truncated": False,
                                               "knowledge_retrieval_applied": False}
                        event_sink.finish_event(retrieve_event, "failed", {**retrieval_metadata, "error_code": "retrieval_unavailable"}, retrieval_metadata["retrieval_duration_ms"])
                    runtime_snapshot.update(retrieval_metadata)
                    _update_runtime_metadata(run_id, runtime_snapshot)
                catalog = module_registry.tool_catalog_view()
                web_enabled = req.web_enabled if "web_enabled" in req.model_fields_set else True
                tools_enabled = req.tools_enabled if "tools_enabled" in req.model_fields_set else True
                allowed_tools = {entry.name for entry in catalog.entries() if (entry.name == "web_search" and web_enabled) or (entry.name != "web_search" and tools_enabled)}
                effective_tools = exposure_policy.resolve(catalog, {"tool-calling"} if supports_tools else set(), profile_config.requested_tool_names if profile_config else None, allowed_tools)
                effective_tool_names = [tool["function"]["name"] for tool in effective_tools.definitions()]
                runtime_snapshot["effective_tool_names"] = effective_tool_names
                if profile_config and profile_config.temperature is not None:
                    runtime_snapshot["temperature"] = profile_config.temperature
                elif req.temperature is not None:
                    runtime_snapshot["temperature"] = req.temperature
                _update_runtime_metadata(run_id, runtime_snapshot)
                effective_configuration = EffectiveRunConfiguration(
                    profile_config,
                    effective_tools,
                    grounded_context,
                    profile_config.temperature if profile_config else req.temperature,
                )
                runtime_snapshot["physical_message_roles"] = (["system"] if profile_config else []) + (["system"] if grounded_context else []) + [message["role"] for message in messages]
                _update_runtime_metadata(run_id, runtime_snapshot)
                run_request = AgentRunRequest(model_adapter, messages, effective_tools, ToolExecutor(), execution_context,
                                              profile_config.temperature if profile_config else req.temperature, event_sink,
                                              profile_config.system_instructions if profile_config else "",
                                              profile_config.profile_id if profile_config else None,
                                               profile_config.profile_name if profile_config else None,
                                               runtime_snapshot, grounded_context, effective_configuration)
                async for event in agent_runtime.stream(run_request):
                    if "trace" in event:
                        trace = event["trace"]
                        _insert_trace_event(cid, message_id, trace["type"], trace["status"], trace.get("metadata", {}), trace.get("duration_ms"))
                        yield "data: " + json.dumps({"trace": trace}) + "\n\n"
                        continue
                    if "activity" in event:
                        yield "data: " + json.dumps({"activity": event["activity"]}) + "\n\n"
                        continue
                    if "delta" in event:
                        yield "data: " + json.dumps({"delta": event["delta"]}) + "\n\n"
                    elif "status" in event:
                        yield "data: " + json.dumps({"status": event["status"], "message": event["message"]}) + "\n\n"
                    elif "error" in event:
                        run_status = "failed"
                        yield "data: " + json.dumps({"error": event["error"]}) + "\n\n"
                        return
                    elif event.get("complete"):
                        answer, sources = event["answer"], event["sources"]
                        telemetry = {key: value for key, value in event.get("telemetry", {}).items() if value is not None}
                        runtime_metadata = {**runtime_snapshot, **telemetry}
                        _update_runtime_metadata(run_id, runtime_metadata)
                        execution = {"tools_used": event.get("tools_used", []), "tool_rounds": event.get("tool_rounds", 0), "web_search_used": "web_search" in event.get("tools_used", [])}
                        if not execution_future.done():
                            execution_future.set_result(execution)
                        run_status = "completed"
            notebook_citations = cited_results(answer, grounded_context) if grounded_context else []
            cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
            sources = [source for index, source in enumerate(sources, 1) if index in cited]
            runtime_metadata["citation_count"] = len(notebook_citations)
            assistant_id = str(uuid.uuid4())
            with db() as c:
                c.execute("INSERT INTO messages(id,conversation_id,role,content,provider_id,model_id,attachments,sources,runtime_metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (assistant_id, cid, "assistant", answer, selected_provider_id, selected_model_id, "[]", json.dumps(sources), json.dumps(runtime_metadata), now()))
                for citation_key, result in notebook_citations:
                    c.execute("""INSERT INTO message_citations
                        (id,message_id,citation_key,notebook_id,source_id,document_id,chunk_id,canonical_start,canonical_end,provenance,document_content_hash,chunk_content_hash)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (str(uuid.uuid4()), assistant_id, citation_key, notebook_id, result["source_id"], result["document_id"], result["chunk_id"], result["canonical_start"], result["canonical_end"], json.dumps(result.get("provenance", [])), result.get("document_content_hash"), result.get("chunk_content_hash")))
                c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
            _update_runtime_metadata(run_id, runtime_metadata)
            module_registry.run_hook("chat_after", {"conversation_id": cid, "provider_id": selected_provider_id, "model_id": selected_model_id})
            citation_payload = [{"citation_key": citation_key, **{key: value for key, value in result.items() if key != "content"}} for citation_key, result in notebook_citations]
            yield "data: " + json.dumps({"done": True, "conversation_id": cid, "provider_id": selected_provider_id, "model_id": selected_model_id, "sources": sources, "citations": citation_payload, "runtime": runtime_metadata}) + "\n\n"
        except httpx.RequestError as e:
            run_status = "failed"
            yield "data: " + json.dumps({"error": f"Provider connection failed: {str(e)[:180]}"}) + "\n\n"
        finally:
            _finish_runtime_run(run_id, locals().get("run_status", "failed"))
            if not execution_future.done():
                execution_future.set_result({"tools_used": [], "tool_rounds": 0, "web_search_used": False})
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})


def _shadow_configured() -> bool:
    service = module_registry.service("decision-shadow")
    return bool(getattr(service, "shadow_enabled", False)) or str(os.getenv("NEXO_DECISION_SHADOW", "false")).strip().lower() in {"1", "true", "yes", "on"}


def _tool_calling_fallback() -> bool:
    return str(os.getenv("NEXO_TOOL_CALLING_FALLBACK", "false")).strip().lower() in {"1", "true", "yes", "on"}


def _finish_shadow_task(task: asyncio.Task[None], conversation_id: str, message_id: str) -> None:
    shadow_tasks.discard(task)
    if task.cancelled():
        diagnostic(logger, "shadow_failed", conversation_id=conversation_id, message_id=message_id, error_code="shadow_cancelled")
    elif task.exception() is not None:
        diagnostic(logger, "shadow_failed", conversation_id=conversation_id, message_id=message_id, error_code="shadow_task_failed")


class SQLiteRuntimeEventSink:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def start_event(self, kind: str, name: str, metadata: dict[str, Any] | None = None, parent_event_id: str | None = None) -> str:
        event_id = str(uuid.uuid4())
        try:
            with db() as c:
                sequence = c.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM runtime_events WHERE run_id=?", (self.run_id,)).fetchone()[0]
                c.execute("INSERT INTO runtime_events(id,run_id,sequence,parent_event_id,kind,name,started_at,status,safe_metadata) VALUES(?,?,?,?,?,?,?,?,?)", (event_id, self.run_id, sequence, parent_event_id, kind, name, now(), "running", json.dumps(safe_metadata(metadata))))
        except sqlite3.Error:
            logger.exception("runtime event start failed", extra={"run_id": self.run_id, "kind": kind})
            return ""
        return event_id

    def finish_event(self, event_id: str | None, status: str, metadata: dict[str, Any] | None = None, duration_ms: float | None = None) -> None:
        if not event_id:
            return
        status = {"success": "completed", "ok": "completed", "error": "failed", "tool_execution_error": "failed", "tool_not_available": "failed", "invalid_arguments": "failed"}.get(status, status)
        try:
            with db() as c:
                c.execute("UPDATE runtime_events SET completed_at=?,duration_ms=?,status=?,safe_metadata=? WHERE id=?", (now(), duration_ms, status, json.dumps(safe_metadata(metadata)), event_id))
        except sqlite3.Error:
            logger.exception("runtime event finalization failed", extra={"run_id": self.run_id, "event_id": event_id})


def _finish_runtime_run(run_id: str, status: str) -> None:
    try:
        with db() as c:
            c.execute("UPDATE runtime_runs SET completed_at=?,status=? WHERE id=? AND completed_at IS NULL", (now(), status, run_id))
    except sqlite3.Error:
        logger.exception("runtime run finalization failed", extra={"run_id": run_id})


def _update_runtime_metadata(run_id: str, metadata: dict[str, Any]) -> None:
    try:
        with db() as c:
            row = c.execute("SELECT metadata FROM runtime_runs WHERE id=?", (run_id,)).fetchone()
            current = json.loads(row[0] or "{}") if row else {}
            current.update(safe_metadata(metadata))
            c.execute("UPDATE runtime_runs SET metadata=? WHERE id=?", (json.dumps(current), run_id))
    except (sqlite3.Error, TypeError, ValueError):
        logger.exception("runtime metadata update failed", extra={"run_id": run_id})


def _insert_trace_event(conversation_id: str, message_id: str, event_type: str, status: str, metadata: dict[str, Any], duration_ms: float | None = None) -> str:
    event_id = str(uuid.uuid4())
    with db() as c:
        sequence = c.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM runtime_trace_events WHERE message_id=?", (message_id,)).fetchone()[0]
        c.execute("INSERT INTO runtime_trace_events VALUES(?,?,?,?,?,?,?,?,?)", (event_id, conversation_id, message_id, sequence, event_type, now(), duration_ms, status, json.dumps(safe_metadata(metadata))))
    return event_id


def _update_trace_event(event_id: str, status: str, metadata: dict[str, Any], duration_ms: float | None = None) -> None:
    with db() as c:
        c.execute("UPDATE runtime_trace_events SET status=?,duration_ms=?,metadata=? WHERE id=?", (status, duration_ms, json.dumps(safe_metadata(metadata)), event_id))


async def _run_shadow_observation(message_id: str, conversation_id: str, message: str, tools: list[dict[str, Any]], execution_future: asyncio.Future[dict[str, Any]], trace_id: str | None = None, run_id: str | None = None, event_sink: RuntimeEventSink | None = None, event_id: str | None = None) -> None:
    started = datetime.now(timezone.utc)
    service = module_registry.service("decision-shadow")
    result = ShadowDecision("shadow", None, {}, {}, None)
    error = None
    try:
        if service is None or not getattr(service, "shadow_enabled", False):
            raise RuntimeError("decision_runtime_unavailable")
        diagnostic(logger, "shadow_started", conversation_id=conversation_id, message_id=message_id, latency_ms=None, error_code=None)
        result = await service.shadow_decide(message, sorted({str(tool.get("name")) for tool in tools if tool.get("name")}))
        if trace_id:
            _update_trace_event(trace_id, "success", {"model": result.model, "run_id": run_id, "answers": result.answers, "confidence": {key: answer.get("confidence") for key, answer in result.answers.items()}, "probabilities": {key: answer.get("probabilities") for key, answer in result.answers.items() if answer.get("probabilities")}}, result.latency_ms)
        if event_sink:
            event_sink.finish_event(event_id, "completed", {"model": result.model, "answers": result.answers, "confidence": {key: answer.get("confidence") for key, answer in result.answers.items()}}, result.latency_ms)
        diagnostic(logger, "shadow_completed", conversation_id=conversation_id, message_id=message_id, latency_ms=result.latency_ms, error_code=None)
    except Exception as exc:
        error = getattr(exc, "code", None) or (str(exc) if str(exc) in {"decision_runtime_unavailable"} else "shadow_decision_failed")
        result = ShadowDecision("shadow", None, {}, {}, round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 2), error)
        if trace_id:
            _update_trace_event(trace_id, "error", {"error": error, "run_id": run_id}, result.latency_ms)
        if event_sink:
            event_sink.finish_event(event_id, "failed", {"error_code": error}, result.latency_ms)
        diagnostic(logger, "shadow_failed", conversation_id=conversation_id, message_id=message_id, error_code=error, latency_ms=result.latency_ms)
    try:
        execution = await asyncio.shield(execution_future)
    except asyncio.CancelledError:
        diagnostic(logger, "shadow_failed", conversation_id=conversation_id, message_id=message_id, error_code="shadow_cancelled")
        raise
    except Exception:
        diagnostic(logger, "shadow_failed", conversation_id=conversation_id, message_id=message_id, error_code="execution_observation_failed")
        execution = {"tools_used": [], "tool_rounds": 0, "web_search_used": False}
    with db() as c:
        c.execute("INSERT INTO shadow_observations(id,conversation_id,message_id,mode,model,answers,execution,metadata,latency_ms,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), conversation_id, message_id, result.mode, result.model, json.dumps(result.answers), json.dumps(execution), json.dumps(result.metadata), result.latency_ms, error, now()))
    diagnostic(logger, "shadow_persisted", conversation_id=conversation_id, message_id=message_id, latency_ms=result.latency_ms, error_code=error)


app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    candidate = WEB_DIR / path
    if path and candidate.is_file(): return FileResponse(candidate)
    return FileResponse(WEB_DIR / "index.html")
