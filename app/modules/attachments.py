from __future__ import annotations

import base64
import io
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.kernel import InterfaceExtension, ModuleContext, ModuleManifest


class AttachmentsModule:
    manifest = ModuleManifest(
        id="attachments",
        name="File attachments",
        version="1.0.0",
        api_version=1,
        capabilities=("file-upload", "file-extraction"),
    )
    interface_extensions = (
        InterfaceExtension(id="attach-files", kind="composer-control", label="Adjuntar archivo"),
    )

    def register(self, context: ModuleContext) -> None:
        router = APIRouter(prefix="/api/files", tags=["attachments"])
        max_upload = context.settings["max_upload"]

        @router.post("")
        async def upload_file(file: UploadFile = File(...)):
            data = await file.read(max_upload + 1)
            if len(data) > max_upload:
                raise HTTPException(413, "File exceeds upload limit")
            mime = file.content_type or "application/octet-stream"
            name = Path(file.filename or "attachment").name[:180]
            if mime.startswith("image/"):
                return {"name": name, "mime": mime, "kind": "image", "data_url": "data:" + mime + ";base64," + base64.b64encode(data).decode()}
            if mime.startswith("text/") or name.lower().endswith((".md", ".csv", ".json", ".log")):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    raise HTTPException(415, "Text file must be UTF-8")
                return {"name": name, "mime": mime, "kind": "text", "text": text[:120000]}
            if name.lower().endswith(".pdf") or mime == "application/pdf":
                try:
                    from pypdf import PdfReader
                    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
                except Exception as exc:
                    raise HTTPException(422, f"Could not read PDF: {str(exc)[:120]}")
                if not text.strip():
                    raise HTTPException(422, "This PDF has no selectable text; scanned document OCR will be added later")
                return {"name": name, "mime": "application/pdf", "kind": "text", "text": text[:120000]}
            if name.lower().endswith(".docx"):
                try:
                    from docx import Document
                    text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
                except Exception as exc:
                    raise HTTPException(422, f"Could not read Word document: {str(exc)[:120]}")
                return {"name": name, "mime": mime, "kind": "text", "text": text[:120000]}
            raise HTTPException(415, "Supported files: images, PDF, DOCX, UTF-8 text, Markdown, CSV, JSON, and log files")

        context.app.include_router(router)

    def startup(self, context: ModuleContext) -> None:
        return None

    def shutdown(self, context: ModuleContext) -> None:
        return None

    def run_hook(self, hook: str, context: ModuleContext, payload: object) -> None:
        return None
