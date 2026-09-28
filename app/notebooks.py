from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


SOURCE_TYPES = {"file", "web"}
SOURCE_STATUSES = {"added", "pending", "extracting", "ready", "failed"}


class NotebookValidationError(ValueError):
    pass


class NotebookNotFoundError(LookupError):
    pass


class NotebookSourceNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class NotebookInput:
    name: str
    description: str = ""


class NotebookRepository:
    def __init__(self, connection_factory: Callable[[], sqlite3.Connection], now: Callable[[], str]) -> None:
        self.connection_factory, self.now = connection_factory, now

    def _notebook(self, connection: sqlite3.Connection, notebook_id: str):
        return connection.execute("SELECT * FROM notebooks WHERE id=?", (notebook_id,)).fetchone()

    def get(self, notebook_id: str):
        with self.connection_factory() as connection:
            return self._notebook(connection, notebook_id)

    def list(self):
        with self.connection_factory() as connection:
            return connection.execute("SELECT * FROM notebooks ORDER BY updated_at DESC, name, id").fetchall()

    def create(self, item: NotebookInput):
        notebook_id, timestamp = str(uuid.uuid4()), self.now()
        with self.connection_factory() as connection:
            connection.execute("INSERT INTO notebooks(id,name,description,created_at,updated_at) VALUES(?,?,?,?,?)",
                               (notebook_id, item.name, item.description, timestamp, timestamp))
            return self._notebook(connection, notebook_id)

    def update(self, notebook_id: str, values: dict[str, str]):
        with self.connection_factory() as connection:
            if self._notebook(connection, notebook_id) is None:
                raise NotebookNotFoundError(notebook_id)
            connection.execute("UPDATE notebooks SET name=?,description=?,updated_at=? WHERE id=?",
                               (values["name"], values["description"], self.now(), notebook_id))
            return self._notebook(connection, notebook_id)

    def delete(self, notebook_id: str) -> bool:
        with self.connection_factory() as connection:
            return connection.execute("DELETE FROM notebooks WHERE id=?", (notebook_id,)).rowcount > 0

    def sources(self, notebook_id: str):
        with self.connection_factory() as connection:
            return connection.execute("SELECT * FROM notebook_sources WHERE notebook_id=? ORDER BY created_at, id", (notebook_id,)).fetchall()

    def source(self, notebook_id: str, source_id: str):
        with self.connection_factory() as connection:
            return connection.execute("SELECT * FROM notebook_sources WHERE notebook_id=? AND id=?", (notebook_id, source_id)).fetchone()

    def add_source(self, notebook_id: str, source_type: str, title: str, metadata: dict, content_hash: str | None, source_id: str | None = None):
        source_id, timestamp = source_id or str(uuid.uuid4()), self.now()
        with self.connection_factory() as connection:
            connection.execute("""INSERT INTO notebook_sources
                (id,notebook_id,type,title,status,metadata,content_hash,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (source_id, notebook_id, source_type, title, "added", json.dumps(metadata), content_hash, timestamp, timestamp))
            return connection.execute("SELECT * FROM notebook_sources WHERE id=?", (source_id,)).fetchone()

    def delete_source(self, notebook_id: str, source_id: str):
        with self.connection_factory() as connection:
            row = connection.execute("SELECT * FROM notebook_sources WHERE notebook_id=? AND id=?", (notebook_id, source_id)).fetchone()
            if row is None:
                raise NotebookSourceNotFoundError(source_id)
            connection.execute("DELETE FROM notebook_sources WHERE notebook_id=? AND id=?", (notebook_id, source_id))
            return row


class NotebookService:
    def __init__(self, repository: NotebookRepository, storage_root: Path) -> None:
        self.repository, self.storage_root = repository, storage_root
        self.storage_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate(item: NotebookInput) -> NotebookInput:
        name = item.name.strip()
        if not 1 <= len(name) <= 120:
            raise NotebookValidationError("name must be 1-120 characters")
        if len(item.description) > 2000:
            raise NotebookValidationError("description must be at most 2000 characters")
        return NotebookInput(name, item.description.strip())

    @staticmethod
    def validate_source(source_type: str, title: str, metadata: dict) -> tuple[str, str, dict]:
        source_type, title = source_type.strip().lower(), title.strip()
        if source_type not in SOURCE_TYPES:
            raise NotebookValidationError("source type must be file or web")
        if not 1 <= len(title) <= 240:
            raise NotebookValidationError("title must be 1-240 characters")
        if not isinstance(metadata, dict):
            raise NotebookValidationError("metadata must be an object")
        if source_type == "web":
            url = metadata.get("url", "")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise NotebookValidationError("url must use http or https")
            metadata = {**metadata, "url": url}
        return source_type, title, metadata

    @staticmethod
    def _output(row) -> dict:
        result = dict(row)
        if "metadata" in result:
            result["metadata"] = json.loads(result["metadata"] or "{}")
        return result

    def list(self) -> list[dict]:
        return [self._output(row) | {"source_count": self._source_count(row["id"])} for row in self.repository.list()]

    def _source_count(self, notebook_id: str) -> int:
        return len(self.repository.sources(notebook_id))

    def get(self, notebook_id: str) -> dict:
        row = self.repository.get(notebook_id)
        if row is None:
            raise NotebookNotFoundError(notebook_id)
        return self._output(row) | {"source_count": self._source_count(notebook_id)}

    def create(self, item: NotebookInput) -> dict:
        return self.get(self.repository.create(self.validate(item))["id"])

    def update(self, notebook_id: str, values: dict[str, str]) -> dict:
        current = self.get(notebook_id)
        merged = self.validate(NotebookInput(values.get("name", current["name"]), values.get("description", current["description"])))
        return self._output(self.repository.update(notebook_id, {"name": merged.name, "description": merged.description})) | {"source_count": self._source_count(notebook_id)}

    def delete(self, notebook_id: str) -> None:
        sources = self.repository.sources(notebook_id)
        if not self.repository.delete(notebook_id):
            raise NotebookNotFoundError(notebook_id)
        for source in sources:
            self._remove_file(source["id"])

    def sources(self, notebook_id: str) -> list[dict]:
        self.get(notebook_id)
        return [self._output(row) for row in self.repository.sources(notebook_id)]

    def source(self, notebook_id: str, source_id: str) -> dict:
        self.get(notebook_id)
        row = self.repository.source(notebook_id, source_id)
        if row is None:
            raise NotebookSourceNotFoundError(source_id)
        return self._output(row)

    def add_file(self, notebook_id: str, title: str, filename: str, mime: str, data: bytes) -> dict:
        self.get(notebook_id)
        if not filename or "\x00" in filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise NotebookValidationError("unsafe filename")
        metadata = {"filename": filename, "mime": mime or "application/octet-stream", "size": len(data)}
        source_type, title, metadata = self.validate_source("file", title or filename, metadata)
        source_id = str(uuid.uuid4())
        content_hash = hashlib.sha256(data).hexdigest()
        path = self._path(source_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        try:
            row = self.repository.add_source(notebook_id, source_type, title, metadata, content_hash, source_id)
            return self._output(row)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def add_web(self, notebook_id: str, title: str, url: str, metadata: dict | None = None) -> dict:
        self.get(notebook_id)
        source_type, title, metadata = self.validate_source("web", title or url, {**(metadata or {}), "url": url.strip()})
        return self._output(self.repository.add_source(notebook_id, source_type, title, metadata, None))

    def delete_source(self, notebook_id: str, source_id: str) -> None:
        row = self.repository.delete_source(notebook_id, source_id)
        self._remove_file(row["id"])

    def _path(self, source_id: str) -> Path:
        return self.storage_root / source_id[:2] / source_id

    def _remove_file(self, source_id: str) -> None:
        path = self._path(source_id)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
            self.storage_root.rmdir()
        except OSError:
            pass
