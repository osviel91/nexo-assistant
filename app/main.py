from __future__ import annotations

import base64
import json
import os
import sqlite3
import uuid
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA_DIR = Path(os.getenv("NEXO_DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "nexo.sqlite3"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_UPLOAD = int(os.getenv("NEXO_MAX_UPLOAD_MB", "15")) * 1024 * 1024
app = FastAPI(title="Nexo Chat", version="0.1.0")


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
          label TEXT NOT NULL, PRIMARY KEY(provider_id,id)
        );
        CREATE TABLE IF NOT EXISTS conversations (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          role TEXT NOT NULL, content TEXT NOT NULL, provider_id TEXT, model_id TEXT,
          attachments TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
        );
        """)


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = ""
    models: list[str] = []


class ChatIn(BaseModel):
    conversation_id: str | None = None
    provider_id: str
    model_id: str
    content: str
    attachments: list[dict[str, Any]] = []
    temperature: float | None = None


def provider_dict(row: sqlite3.Row) -> dict[str, Any]:
    with db() as c:
        models = c.execute("SELECT id,label FROM models WHERE provider_id=? ORDER BY label", (row["id"],)).fetchall()
    return {"id": row["id"], "name": row["name"], "base_url": row["base_url"],
            "has_api_key": bool(row["api_key"]), "models": [dict(m) for m in models]}


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version}


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
        c.executemany("INSERT OR IGNORE INTO models VALUES(?,?,?)", [(m, pid, m) for m in item.models if m.strip()])
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
            if m.strip(): c.execute("INSERT OR IGNORE INTO models VALUES(?,?,?)", (m.strip(), pid, m.strip()))
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
    ids = sorted({str(m["id"]) for m in data if isinstance(m, dict) and m.get("id")})
    with db() as c:
        for m in ids: c.execute("INSERT OR IGNORE INTO models VALUES(?,?,?)", (m, pid, m))
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
    return {"conversation": dict(conv), "messages": [{**dict(m), "attachments": json.loads(m["attachments"])} for m in msgs]}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str):
    with db() as c: c.execute("DELETE FROM conversations WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/files")
async def upload_file(file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD: raise HTTPException(413, "File exceeds upload limit")
    mime = file.content_type or "application/octet-stream"
    name = Path(file.filename or "attachment").name[:180]
    if mime.startswith("image/"):
        return {"name": name, "mime": mime, "kind": "image", "data_url": f"data:{mime};base64," + base64.b64encode(data).decode()}
    if mime.startswith("text/") or name.lower().endswith((".md", ".csv", ".json", ".log")):
        try: text = data.decode("utf-8")
        except UnicodeDecodeError: raise HTTPException(415, "Text file must be UTF-8")
        return {"name": name, "mime": mime, "kind": "text", "text": text[:120000]}
    if name.lower().endswith(".pdf") or mime == "application/pdf":
        try:
            from pypdf import PdfReader
            text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        except Exception as e:
            raise HTTPException(422, f"Could not read PDF: {str(e)[:120]}")
        if not text.strip(): raise HTTPException(422, "This PDF has no selectable text; scanned document OCR will be added later")
        return {"name": name, "mime": "application/pdf", "kind": "text", "text": text[:120000]}
    if name.lower().endswith(".docx"):
        try:
            from docx import Document
            doc = Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
        except Exception as e:
            raise HTTPException(422, f"Could not read Word document: {str(e)[:120]}")
        return {"name": name, "mime": mime, "kind": "text", "text": text[:120000]}
    raise HTTPException(415, "Supported files: images, PDF, DOCX, UTF-8 text, Markdown, CSV, JSON, and log files")


@app.post("/api/chat")
async def chat(req: ChatIn):
    if not req.content.strip() and not req.attachments: raise HTTPException(400, "Message is empty")
    with db() as c:
        provider = c.execute("SELECT * FROM providers WHERE id=?", (req.provider_id,)).fetchone()
        if not provider: raise HTTPException(404, "Provider not found")
        if not c.execute("SELECT 1 FROM models WHERE provider_id=? AND id=?", (req.provider_id, req.model_id)).fetchone():
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
        c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), cid, "user", req.content, req.provider_id, req.model_id, json.dumps(req.attachments), now()))
        c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": user_content})
        url, key = provider["base_url"].rstrip("/") + "/chat/completions", provider["api_key"]

    async def events():
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload = {"model": req.model_id, "messages": messages, "stream": True}
        if req.temperature is not None: payload["temperature"] = req.temperature
        answer = ""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode(errors="replace")[:800]
                        yield "data: " + json.dumps({"error": f"Provider HTTP {response.status_code}: {body}"}) + "\n\n"
                        return
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"): continue
                        raw = line[5:].strip()
                        if raw == "[DONE]": break
                        try:
                            obj = json.loads(raw)
                            delta = obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if isinstance(delta, list): delta = "".join(p.get("text", "") for p in delta if isinstance(p, dict))
                            if delta:
                                answer += delta
                                yield "data: " + json.dumps({"delta": delta}) + "\n\n"
                        except (ValueError, IndexError, AttributeError): continue
            with db() as c:
                c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), cid, "assistant", answer, req.provider_id, req.model_id, "[]", now()))
                c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
            yield "data: " + json.dumps({"done": True, "conversation_id": cid, "provider_id": req.provider_id, "model_id": req.model_id}) + "\n\n"
        except httpx.RequestError as e:
            yield "data: " + json.dumps({"error": f"Provider connection failed: {str(e)[:180]}"}) + "\n\n"
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})


app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    candidate = WEB_DIR / path
    if path and candidate.is_file(): return FileResponse(candidate)
    return FileResponse(WEB_DIR / "index.html")
