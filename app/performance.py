from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class RetrievalTelemetry:
    """Safe, optional timings and counts for one retrieval execution."""

    query_analysis_ms: float | None = None
    embedding_ms: float | None = None
    dense_search_ms: float | None = None
    lexical_search_ms: float | None = None
    fusion_ms: float | None = None
    reranking_ms: float | None = None
    relevance_gate_ms: float | None = None
    grounded_context_ms: float | None = None
    retrieval_total_ms: float | None = None
    query_variant_count: int = 0
    dense_search_count: int = 0
    dense_search_total_ms: float = 0.0
    dense_search_max_ms: float | None = None
    lexical_search_count: int = 0
    lexical_search_total_ms: float = 0.0
    lexical_search_max_ms: float | None = None
    query_variants: int = 0
    dense_candidates: int = 0
    lexical_candidates: int = 0
    fused_candidates: int = 0
    reranker_input_candidates: int = 0
    reranked_candidates: int = 0
    retrieved_candidates: int = 0
    relevant_candidates: int = 0
    grounding_candidates: int = 0
    cited_sources: int = 0
    vector_store: str = "local"
    retrieval_mode: str = "hybrid"
    reranker_enabled: bool = False
    reranker_provider: str | None = None
    reranker_model: str | None = None

    @staticmethod
    def elapsed(start: float) -> float:
        return round((time.perf_counter() - start) * 1000, 2)

    def add_search(self, kind: str, duration_ms: float) -> None:
        count_name = f"{kind}_search_count"
        total_name = f"{kind}_search_total_ms"
        max_name = f"{kind}_search_max_ms"
        setattr(self, count_name, getattr(self, count_name) + 1)
        setattr(self, total_name, round(getattr(self, total_name) + duration_ms, 2))
        current_max = getattr(self, max_name)
        setattr(self, max_name, duration_ms if current_max is None else max(current_max, duration_ms))

    def as_dict(self) -> dict[str, Any]:
        values = {key: value for key, value in self.__dict__.items() if value is not None}
        values.update({
            "embedding_duration_ms": values.get("embedding_ms"),
            "dense_duration_ms": values.get("dense_search_ms"),
            "lexical_duration_ms": values.get("lexical_search_ms"),
            "fusion_duration_ms": values.get("fusion_ms"),
            "rerank_duration_ms": values.get("reranking_ms"),
            "relevance_gate_duration_ms": values.get("relevance_gate_ms"),
            "grounded_context_duration_ms": values.get("grounded_context_ms"),
            "retrieval_duration_ms": values.get("retrieval_total_ms"),
            "dense_candidate_count": self.dense_candidates,
            "lexical_candidate_count": self.lexical_candidates,
            "fused_candidate_count": self.fused_candidates,
            "reranker_candidate_count": self.reranker_input_candidates,
            "reranked_candidate_count": self.reranked_candidates,
            "retrieved_candidate_count": self.retrieved_candidates,
            "relevant_candidate_count": self.relevant_candidates,
        })
        return values
