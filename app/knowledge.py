from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Any


class KnowledgeConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class EmbeddingConfiguration:
    id: str
    provider_id: str
    model_id: str
    target_chunk_size: int
    max_chunk_size: int
    overlap: int
    batch_size: int
    retrieval_top_k: int
    retrieval_max_context_chars: int
    config_version: int
    created_at: str
    updated_at: str

    @property
    def chunking_semantics(self) -> str:
        return "whitespace_estimate"

    def chunking_hash(self) -> str:
        payload = json.dumps({"target": self.target_chunk_size, "max": self.max_chunk_size, "overlap": self.overlap}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def public(self) -> dict[str, Any]:
        return asdict(self) | {"chunking_semantics": self.chunking_semantics, "units": "estimated whitespace tokens"}


def validate_configuration(values: dict[str, Any]) -> dict[str, Any]:
    limits = {
        "target_chunk_size": (1, 10000), "max_chunk_size": (1, 20000), "overlap": (0, 5000),
        "batch_size": (1, 256), "retrieval_top_k": (1, 50), "retrieval_max_context_chars": (1000, 1000000),
    }
    result = dict(values)
    for key, (low, high) in limits.items():
        try:
            value = int(result[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise KnowledgeConfigurationError(f"{key} must be an integer") from exc
        if not low <= value <= high:
            raise KnowledgeConfigurationError(f"{key} must be between {low} and {high}")
        result[key] = value
    if result["target_chunk_size"] > result["max_chunk_size"]:
        raise KnowledgeConfigurationError("target_chunk_size must not exceed max_chunk_size")
    if result["overlap"] >= result["max_chunk_size"]:
        raise KnowledgeConfigurationError("overlap must be smaller than max_chunk_size")
    if not str(result.get("provider_id", "")).strip() or not str(result.get("model_id", "")).strip():
        raise KnowledgeConfigurationError("provider_id and model_id are required")
    return result


def bootstrap_values() -> dict[str, Any]:
    return {
        "provider_id": os.getenv("NEXO_EMBEDDING_PROVIDER_ID", ""),
        "model_id": os.getenv("NEXO_EMBEDDING_MODEL", "embedding-model"),
        "target_chunk_size": os.getenv("NEXO_EMBEDDING_TARGET_CHUNK_SIZE", "400"),
        "max_chunk_size": os.getenv("NEXO_EMBEDDING_MAX_CHUNK_SIZE", "600"),
        "overlap": os.getenv("NEXO_EMBEDDING_OVERLAP", "40"),
        "batch_size": os.getenv("NEXO_EMBEDDING_BATCH_SIZE", "32"),
        "retrieval_top_k": os.getenv("NEXO_RAG_TOP_K", "5"),
        "retrieval_max_context_chars": os.getenv("NEXO_RAG_MAX_CONTEXT_CHARS", "12000"),
    }
