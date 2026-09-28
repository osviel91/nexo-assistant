from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChunkingConfig:
    target_tokens: int = 400
    max_tokens: int = 600
    overlap_tokens: int = 40

    def __post_init__(self) -> None:
        if not 1 <= self.target_tokens <= self.max_tokens:
            raise ValueError("target_tokens must be positive and no greater than max_tokens")
        if not 0 <= self.overlap_tokens < self.max_tokens:
            raise ValueError("overlap_tokens must be smaller than max_tokens")


@dataclass(frozen=True)
class DocumentChunk:
    id: str
    notebook_id: str
    document_id: str
    source_id: str
    ordinal: int
    content: str
    canonical_start: int
    canonical_end: int
    token_count: int | None
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""


def estimated_tokens(text: str) -> int:
    """Whitespace token estimate; it is deliberately not presented as model tokens."""
    return len(re.findall(r"\S+", text))


def _units(document: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    content = document["content"]
    source_type = document.get("metadata", {}).get("source_type", document.get("source_type", ""))
    if source_type == "file":
        source_type = {"text/markdown": "markdown", "application/pdf": "pdf"}.get(document.get("content_type"), "text")
    spans = document.get("spans", [])
    structural: list[tuple[int, int, dict[str, Any]]] = []
    if any(s.get("source_location", {}).get("heading") for s in spans):
        source_type = "markdown"
    if source_type == "pdf":
        structural = [(s["start_offset"], s["end_offset"], {"page": s["source_location"].get("page")})
                      for s in spans if s.get("source_location", {}).get("page")]
    elif source_type in {"markdown", "web"}:
        headings = [s for s in spans if s.get("source_location", {}).get("heading")]
        for index, heading in enumerate(headings):
            start = heading["start_offset"]
            end = headings[index + 1]["start_offset"] if index + 1 < len(headings) else len(content)
            structural.append((start, end, {"heading": heading["source_location"]["heading"], "level": heading["source_location"].get("level")}))
    if not structural:
        structural = [(match.start(), match.end(), {}) for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", content, re.S)]
    return structural or [(0, len(content), {})]


def _split(start: int, end: int, content: str, config: ChunkingConfig, metadata: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    words = list(re.finditer(r"\S+", content[start:end]))
    if not words:
        return []
    result = []
    position = 0
    while position < len(words):
        stop = min(len(words), position + config.max_tokens)
        if stop < len(words) and stop - position < config.target_tokens:
            stop = min(len(words), position + config.target_tokens)
        absolute_start = start + words[position].start()
        absolute_end = start + words[stop - 1].end()
        result.append((absolute_start, absolute_end, metadata))
        if stop == len(words):
            break
        position = max(position + 1, stop - config.overlap_tokens)
    return result


def chunk_document(document: dict[str, Any], config: ChunkingConfig | None = None) -> list[DocumentChunk]:
    config = config or ChunkingConfig()
    document_hash = document.get("content_hash", hashlib.sha256(document["content"].encode("utf-8")).hexdigest())
    pieces: list[tuple[int, int, dict[str, Any]]] = []
    for start, end, metadata in _units(document):
        pieces.extend(_split(start, end, document["content"], config, metadata))
    chunks: list[DocumentChunk] = []
    for ordinal, (start, end, structural) in enumerate(pieces):
        text = document["content"][start:end]
        provenance = [
            {"source_type": span["source_type"], "source_location": span["source_location"]}
            for span in document.get("spans", [])
            if span["start_offset"] < end and span["end_offset"] > start
        ]
        metadata = {**structural, "provenance": provenance, "token_count_semantics": "whitespace_estimate",
                    "document_content_hash": document_hash}
        chunks.append(DocumentChunk(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"nexo:{document['id']}:{ordinal}:{hashlib.sha256(text.encode()).hexdigest()}")),
            notebook_id=document["notebook_id"], document_id=document["id"], source_id=document["source_id"],
            ordinal=ordinal, content=text, canonical_start=start, canonical_end=end,
            token_count=estimated_tokens(text), metadata=metadata,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ))
    return chunks
