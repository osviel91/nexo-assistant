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
    retrieval_mode: str = "hybrid"
    dense_candidate_limit: int = 20
    lexical_candidate_limit: int = 20
    rrf_k: int = 60
    final_top_k: int = 5
    reranking_enabled: bool = False
    reranker_provider_id: str = ""
    reranker_model: str = ""
    reranker_candidate_limit: int = 20
    reranker_timeout_ms: int = 3000
    relevance_gate_enabled: bool = True
    relevance_gate_min_term_overlap: int = 1

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
        "dense_candidate_limit": (1, 200), "lexical_candidate_limit": (1, 200), "rrf_k": (1, 1000),
        "final_top_k": (1, 50),
        "reranker_candidate_limit": (1, 200), "reranker_timeout_ms": (1, 120000),
        "relevance_gate_min_term_overlap": (1, 20),
    }
    result = dict(values)
    result.setdefault("retrieval_top_k", 5)
    result.setdefault("retrieval_mode", "hybrid")
    result.setdefault("dense_candidate_limit", 20)
    result.setdefault("lexical_candidate_limit", 20)
    result.setdefault("rrf_k", 60)
    result.setdefault("final_top_k", result["retrieval_top_k"])
    result.setdefault("reranking_enabled", False)
    result.setdefault("reranker_provider_id", "")
    result.setdefault("reranker_model", "")
    result.setdefault("reranker_candidate_limit", 20)
    result.setdefault("reranker_timeout_ms", 3000)
    result.setdefault("relevance_gate_enabled", True)
    result.setdefault("relevance_gate_min_term_overlap", 1)
    if isinstance(result["relevance_gate_enabled"], str):
        result["relevance_gate_enabled"] = result["relevance_gate_enabled"].lower() in {"1", "true", "yes", "on"}
    result["relevance_gate_enabled"] = bool(result["relevance_gate_enabled"])
    if isinstance(result["reranking_enabled"], str):
        result["reranking_enabled"] = result["reranking_enabled"].lower() in {"1", "true", "yes", "on"}
    result["reranking_enabled"] = bool(result["reranking_enabled"])
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
    result["retrieval_mode"] = str(result["retrieval_mode"]).lower()
    if result["retrieval_mode"] not in {"dense", "lexical", "hybrid"}:
        raise KnowledgeConfigurationError("retrieval_mode must be dense, lexical, or hybrid")
    if not str(result.get("provider_id", "")).strip() or not str(result.get("model_id", "")).strip():
        raise KnowledgeConfigurationError("provider_id and model_id are required")
    if result["reranking_enabled"] and (not str(result["reranker_provider_id"]).strip() or not str(result["reranker_model"]).strip()):
        raise KnowledgeConfigurationError("reranker_provider_id and reranker_model are required when reranking is enabled")
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
        "retrieval_mode": os.getenv("NEXO_RAG_MODE", "hybrid"),
        "dense_candidate_limit": os.getenv("NEXO_RAG_DENSE_CANDIDATES", "20"),
        "lexical_candidate_limit": os.getenv("NEXO_RAG_LEXICAL_CANDIDATES", "20"),
        "rrf_k": os.getenv("NEXO_RAG_RRF_K", "60"),
        "final_top_k": os.getenv("NEXO_RAG_TOP_K", "5"),
        "retrieval_max_context_chars": os.getenv("NEXO_RAG_MAX_CONTEXT_CHARS", "12000"),
        "reranking_enabled": os.getenv("NEXO_RERANKING_ENABLED", "false"),
        "reranker_provider_id": os.getenv("NEXO_RERANKER_PROVIDER_ID", ""),
        "reranker_model": os.getenv("NEXO_RERANKER_MODEL", ""),
        "reranker_candidate_limit": os.getenv("NEXO_RERANKER_CANDIDATES", "20"),
        "reranker_timeout_ms": os.getenv("NEXO_RERANKER_TIMEOUT_MS", "3000"),
        "relevance_gate_enabled": os.getenv("NEXO_RELEVANCE_GATE_ENABLED", "true"),
        "relevance_gate_min_term_overlap": os.getenv("NEXO_RELEVANCE_GATE_MIN_TERM_OVERLAP", "1"),
    }
