from __future__ import annotations

import json
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

from app.kernel import ModuleRegistry, enabled_module_ids
from app.modules.attachments import AttachmentsModule
from app.modules.web_search_searxng import WebSearchSearxngModule

DATA_DIR = Path(os.getenv("NEXO_DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "nexo.sqlite3"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_UPLOAD = int(os.getenv("NEXO_MAX_UPLOAD_MB", "15")) * 1024 * 1024
app = FastAPI(title="Nexo Chat", version="0.1.0")
module_registry = ModuleRegistry(app, {"max_upload": MAX_UPLOAD})
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


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.on_event("startup")
def startup() -> None:
    with db() as c:
        c.executescript("""
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
        """)
        model_columns = {row["name"] for row in c.execute("PRAGMA table_info(models)")}
        if "capabilities" not in model_columns:
            c.execute("ALTER TABLE models ADD COLUMN capabilities TEXT NOT NULL DEFAULT '[]'")
        message_columns = {row["name"] for row in c.execute("PRAGMA table_info(messages)")}
        if "sources" not in message_columns:
            c.execute("ALTER TABLE messages ADD COLUMN sources TEXT NOT NULL DEFAULT '[]'")
    module_registry.startup()


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


def provider_dict(row: sqlite3.Row) -> dict[str, Any]:
    with db() as c:
        models = c.execute("SELECT id,label,capabilities FROM models WHERE provider_id=? ORDER BY label", (row["id"],)).fetchall()
    return {"id": row["id"], "name": row["name"], "base_url": row["base_url"],
            "has_api_key": bool(row["api_key"]), "models": [{**dict(m), "capabilities": json.loads(m["capabilities"])} for m in models]}


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version}


@app.get("/api/modules")
def modules():
    return module_registry.catalog()


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
        c.execute("INSERT INTO providers VALUES(?,?,?,?,?)", (pid, item.name.strip(), url, item.api_key, now()))
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


@app.post("/api/providers/{pid}/refresh")
async def refresh_models(pid: str):
    payload = await provider_request(pid, "/models")
    data = payload.get("data", [])
    models = [m for m in data if isinstance(m, dict) and m.get("id")]
    with db() as c:
        for model in models:
            capabilities = model.get("capabilities", [])
            if model.get("supports_tools") is True or model.get("tool_calling") is True:
                capabilities = list(set(capabilities if isinstance(capabilities, list) else []) | {"tool-calling"})
            if not isinstance(capabilities, list): capabilities = []
            if any("tool" in str(item).lower() or "function" in str(item).lower() for item in model.get("supported_parameters", [])):
                capabilities = list(set(capabilities) | {"tool-calling"})
            c.execute("INSERT INTO models(id,provider_id,label,capabilities) VALUES(?,?,?,?) ON CONFLICT(provider_id,id) DO UPDATE SET capabilities=excluded.capabilities", (str(model["id"]), pid, str(model["id"]), json.dumps([str(cap) for cap in capabilities])))
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
        c.execute("INSERT INTO messages(id,conversation_id,role,content,provider_id,model_id,attachments,created_at) VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), cid, "user", req.content, req.provider_id, req.model_id, json.dumps(req.attachments), now()))
        c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": user_content})
        url, key = provider["base_url"].rstrip("/") + "/chat/completions", provider["api_key"]
        supports_tools = "tool-calling" in json.loads(model["capabilities"] or "[]")
    module_registry.run_hook("chat_before", {"conversation_id": cid, "provider_id": req.provider_id, "model_id": req.model_id})

    async def events():
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        answer = ""
        sources: list[dict[str, Any]] = []
        tool_rounds = 0
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
                for request_round in range(4):
                    payload = {"model": req.model_id, "messages": messages, "stream": True}
                    if req.temperature is not None: payload["temperature"] = req.temperature
                    if supports_tools and tool_rounds < 3 and module_registry.tool_definitions():
                        payload["tools"] = module_registry.tool_definitions()
                    tool_calls: dict[int, dict[str, Any]] = {}
                    round_content = ""
                    async with client.stream("POST", url, headers=headers, json=payload) as response:
                        if response.status_code >= 400:
                            yield "data: " + json.dumps({"error": f"Provider HTTP {response.status_code}"}) + "\n\n"
                            return
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"): continue
                            raw = line[5:].strip()
                            if raw == "[DONE]": break
                            try:
                                choice = json.loads(raw).get("choices", [{}])[0]
                                delta = choice.get("delta", {})
                                text = delta.get("content", "")
                                if isinstance(text, list): text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
                                if text:
                                    round_content += text
                                    answer += text
                                    yield "data: " + json.dumps({"delta": text}) + "\n\n"
                                for call in delta.get("tool_calls", []) or []:
                                    index = int(call.get("index", 0))
                                    current = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                                    current["id"] += call.get("id", "") or ""
                                    function = call.get("function", {})
                                    current["name"] += function.get("name", "") or ""
                                    current["arguments"] += function.get("arguments", "") or ""
                            except (ValueError, IndexError, AttributeError, TypeError):
                                continue
                    if not tool_calls:
                        break
                    assistant_call = {"role": "assistant", "content": round_content or None, "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in tool_calls.values()]}
                    messages.append(assistant_call)
                    tool_rounds += 1
                    for call in tool_calls.values():
                        try:
                            arguments = json.loads(call["arguments"] or "{}")
                            if not isinstance(arguments, dict): raise ValueError
                            result = await module_registry.invoke_tool(call["name"], arguments)
                        except KeyError:
                            result = {"error": {"code": "unknown_tool", "message": "Herramienta desconocida."}}
                        except (ValueError, TypeError, json.JSONDecodeError):
                            result = {"error": {"code": "invalid_arguments", "message": "Argumentos de herramienta inválidos."}}
                        result = dict(result)
                        if isinstance(result.get("results"), list):
                            numbered = []
                            for item in result["results"]:
                                item = dict(item)
                                item["source"] = len(sources) + 1
                                source = {"title": item.get("title", "Fuente"), "url": item.get("url", "")}
                                sources.append(source)
                                numbered.append(item)
                            result["results"] = numbered
                        if result.get("error"):
                            yield "data: " + json.dumps({"status": "web_search_error", "message": result["error"].get("message", "Error de búsqueda")}) + "\n\n"
                        messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": json.dumps(result, ensure_ascii=False)})
                else:
                    yield "data: " + json.dumps({"error": "Se alcanzó el límite de rondas de herramientas."}) + "\n\n"
                    return
            cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
            sources = [source for index, source in enumerate(sources, 1) if index in cited]
            with db() as c:
                c.execute("INSERT INTO messages(id,conversation_id,role,content,provider_id,model_id,attachments,sources,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), cid, "assistant", answer, req.provider_id, req.model_id, "[]", json.dumps(sources), now()))
                c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
            module_registry.run_hook("chat_after", {"conversation_id": cid, "provider_id": req.provider_id, "model_id": req.model_id})
            yield "data: " + json.dumps({"done": True, "conversation_id": cid, "provider_id": req.provider_id, "model_id": req.model_id, "sources": sources}) + "\n\n"
        except httpx.RequestError as e:
            yield "data: " + json.dumps({"error": f"Provider connection failed: {str(e)[:180]}"}) + "\n\n"
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})


app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    candidate = WEB_DIR / path
    if path and candidate.is_file(): return FileResponse(candidate)
    return FileResponse(WEB_DIR / "index.html")
