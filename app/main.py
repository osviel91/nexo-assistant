from __future__ import annotations

import json
import asyncio
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import AgentRunRequest, AgentRuntime, AgentRuntimeLimits
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
from app.agent_profiles import AgentProfileInput, AgentProfileRepository, AgentProfileService, ProfileNotFoundError, ProfileValidationError

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
    with db() as c: rows = c.execute("SELECT id,title,created_at,updated_at FROM conversations ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/conversations/{cid}")
def get_conversation(cid: str):
    with db() as c:
        conv = c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if not conv: raise HTTPException(404, "Conversation not found")
        msgs = c.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (cid,)).fetchall()
    return {"conversation": dict(conv), "messages": [{**dict(m), "attachments": json.loads(m["attachments"]), "sources": json.loads(m["sources"])} for m in msgs]}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str):
    with db() as c: c.execute("DELETE FROM conversations WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/chat")
async def chat(req: ChatIn):
    if not req.content.strip() and not req.attachments: raise HTTPException(400, "Message is empty")
    with db() as c:
        provider = c.execute("SELECT * FROM providers WHERE id=?", (req.provider_id,)).fetchone()
        if not provider: raise HTTPException(404, "Provider not found")
        model = c.execute("SELECT * FROM models WHERE provider_id=? AND id=?", (req.provider_id, req.model_id)).fetchone()
        if not model:
            raise HTTPException(400, "Choose a model configured for this provider")
        cid = req.conversation_id or str(uuid.uuid4())
        conv = c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if req.conversation_id and not conv: raise HTTPException(404, "Conversation not found")
        if not conv:
            title = req.content.strip().replace("\n", " ")[:60] or "New chat"
            c.execute("INSERT INTO conversations VALUES(?,?,?,?)", (cid, title, now(), now()))
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
        c.execute("INSERT INTO messages(id,conversation_id,role,content,provider_id,model_id,attachments,created_at) VALUES(?,?,?,?,?,?,?,?)", (message_id, cid, "user", req.content, req.provider_id, req.model_id, json.dumps(req.attachments), now()))
        c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
        c.execute("INSERT INTO runtime_runs(id,conversation_id,message_id,started_at,status,model,metadata) VALUES(?,?,?,?,?,?,?)", (run_id, cid, message_id, now(), "started", req.model_id, json.dumps({"provider": req.provider_id})))
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": user_content})
        url, key = provider["base_url"].rstrip("/") + "/chat/completions", provider["api_key"]
        supports_tools = "tool-calling" in json.loads(model["capabilities"] or "[]")
        diagnostic(logger, "capability_policy", **{
            "model": req.model_id,
            "reported_tool_capability": supports_tools,
            "fallback_enabled": _tool_calling_fallback(),
            "effective_tool_capability": supports_tools,
        })
    execution_context = ToolExecutionContext(cid, req.provider_id, req.model_id, 0, run_id)
    event_sink = SQLiteRuntimeEventSink(run_id)
    module_registry.run_hook("chat_before", {"conversation_id": cid, "provider_id": req.provider_id, "model_id": req.model_id})
    execution_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    if _shadow_configured():
        trace_id = _insert_trace_event(cid, message_id, "DECIDE", "running", {"model": None, "run_id": run_id})
        decide_event_id = event_sink.start_event("DECIDE", "decision shadow", {"round": 0})
        task = asyncio.create_task(_run_shadow_observation(message_id, cid, req.content, module_registry.tool_catalog(), execution_future, trace_id, run_id, event_sink, decide_event_id))
        shadow_tasks.add(task)
        task.add_done_callback(lambda finished: _finish_shadow_task(finished, cid, message_id))

    async def events():
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
                model_adapter = OpenAICompatibleModelAdapter(client, url, headers, req.model_id)
                catalog = module_registry.tool_catalog_view()
                effective_tools = exposure_policy.resolve(catalog, {"tool-calling"} if supports_tools else set())
                run_request = AgentRunRequest(model_adapter, messages, effective_tools, ToolExecutor(), execution_context, req.temperature, event_sink)
                async for event in agent_runtime.stream(run_request):
                    if "trace" in event:
                        trace = event["trace"]
                        _insert_trace_event(cid, message_id, trace["type"], trace["status"], trace.get("metadata", {}), trace.get("duration_ms"))
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
                        execution = {"tools_used": event.get("tools_used", []), "tool_rounds": event.get("tool_rounds", 0), "web_search_used": "web_search" in event.get("tools_used", [])}
                        if not execution_future.done():
                            execution_future.set_result(execution)
                        run_status = "completed"
            cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
            sources = [source for index, source in enumerate(sources, 1) if index in cited]
            with db() as c:
                c.execute("INSERT INTO messages(id,conversation_id,role,content,provider_id,model_id,attachments,sources,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), cid, "assistant", answer, req.provider_id, req.model_id, "[]", json.dumps(sources), now()))
                c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
            module_registry.run_hook("chat_after", {"conversation_id": cid, "provider_id": req.provider_id, "model_id": req.model_id})
            yield "data: " + json.dumps({"done": True, "conversation_id": cid, "provider_id": req.provider_id, "model_id": req.model_id, "sources": sources}) + "\n\n"
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
