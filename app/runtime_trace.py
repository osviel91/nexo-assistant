from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol


SAFE_METADATA_KEYS = {
    "model", "tool", "provider", "round", "status", "duration_ms",
    "error_code", "agent_profile_id", "capabilities", "answers", "confidence", "probabilities",
    "agent_profile_name", "resolved_provider", "resolved_provider_name", "resolved_model", "resolved_model_name", "system_instructions_applied",
    "effective_tool_names", "temperature", "top_p", "top_k", "context_window", "context_used_tokens",
    "context_utilization", "prompt_tokens", "completion_tokens", "total_tokens", "ttft_ms",
    "generation_duration_ms", "total_duration_ms", "tokens_per_second", "usage_source",
    "thinking_duration_ms", "thinking_tokens", "thinking_budget", "thinking_available", "thinking_content_available",
    "tokens_per_second_source",
    "notebook_id", "notebook_name", "notebook_request_state", "requested_notebook_id_present", "conversation_notebook_bound", "effective_notebook_bound", "notebook_resolution_source", "notebook_resolution_status", "knowledge_available", "knowledge_retrieval_enabled", "knowledge_retrieval_applied",
    "knowledge_indexed_sources", "knowledge_embedding_model", "knowledge_outcome", "retrieval_count", "retrieval_duration_ms", "top_score", "top_scores", "selected_chunk_ids", "retrieval_query_sha256", "retrieval_query_length", "grounded_context_created", "grounding_applied", "grounding_chunks", "grounding_message_count", "grounding_message_index", "physical_payload_grounding", "context_chars", "context_truncated", "citation_count",
    "retrieval_status", "retrieval_reason", "retrieval_result_count", "retrieved_candidate_count", "relevant_candidate_count", "relevance_gate_applied", "relevance_gate_status", "relevance_gate_reason", "relevance_gate_min_term_overlap", "grounding_reason", "grounding_context_chars", "embedding_model", "index_status", "physical_message_roles", "generation_model", "soul_applied",
    "retrieval_mode", "configured_retrieval_mode", "effective_retrieval_mode", "dense_attempted", "dense_candidate_count", "dense_failure_reason", "lexical_attempted", "lexical_candidate_count", "lexical_failure_reason", "fts_available", "fused_candidate_count", "final_candidate_count", "fallback_errors",
    "reranking_enabled", "reranker_status", "reranker_reason", "reranker_provider_id", "reranker_model", "reranker_candidate_count", "reranked_candidate_count", "rerank_duration_ms", "dense_duration_ms", "lexical_duration_ms", "fusion_duration_ms", "query_variant_count", "query_expanded", "query_variants", "candidate_scores",
    "total_request_ms", "query_analysis_ms", "embedding_ms", "dense_search_ms", "lexical_search_ms", "fusion_ms", "reranking_ms", "relevance_gate_ms", "grounded_context_ms", "retrieval_total_ms", "provider_ttft_ms", "generation_ms", "dense_search_count", "dense_search_total_ms", "dense_search_max_ms", "lexical_search_count", "lexical_search_total_ms", "lexical_search_max_ms", "query_variants", "dense_candidates", "lexical_candidates", "fused_candidates", "reranker_input_candidates", "reranked_candidates", "retrieved_candidates", "relevant_candidates", "grounding_candidates", "cited_sources", "vector_store", "reranker_enabled", "reranker_provider",
}


def safe_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep observability metadata operational; never copy payload-shaped data."""
    output: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if key not in SAFE_METADATA_KEYS:
            continue
        if key in {"model", "tool", "provider", "status", "error_code", "agent_profile_id", "agent_profile_name", "resolved_provider", "resolved_provider_name", "resolved_model", "resolved_model_name", "notebook_id", "notebook_name", "notebook_request_state", "notebook_resolution_source", "notebook_resolution_status", "knowledge_embedding_model", "knowledge_outcome", "retrieval_query_sha256", "usage_source", "retrieval_status", "retrieval_reason", "relevance_gate_status", "relevance_gate_reason", "grounding_reason", "embedding_model", "index_status", "generation_model", "reranker_status", "reranker_reason", "reranker_provider_id", "reranker_model", "vector_store", "reranker_provider"} and isinstance(value, (str, int, float, bool)):
            output[key] = str(value)
        elif key in {"system_instructions_applied", "knowledge_available", "knowledge_retrieval_enabled", "knowledge_retrieval_applied", "requested_notebook_id_present", "conversation_notebook_bound", "effective_notebook_bound", "reranking_enabled", "dense_attempted", "lexical_attempted", "fts_available", "relevance_gate_applied"} and isinstance(value, bool):
            output[key] = value
        elif key in {"round", "duration_ms", "knowledge_indexed_sources", "retrieval_count", "retrieval_result_count", "retrieved_candidate_count", "relevant_candidate_count", "relevance_gate_min_term_overlap", "retrieval_duration_ms", "dense_candidate_count", "lexical_candidate_count", "fused_candidate_count", "final_candidate_count", "reranker_candidate_count", "reranked_candidate_count", "rerank_duration_ms", "dense_duration_ms", "lexical_duration_ms", "fusion_duration_ms", "query_variant_count", "query_analysis_ms", "embedding_ms", "dense_search_ms", "lexical_search_ms", "fusion_ms", "reranking_ms", "relevance_gate_ms", "grounded_context_ms", "retrieval_total_ms", "provider_ttft_ms", "generation_ms", "total_request_ms", "dense_search_count", "dense_search_total_ms", "dense_search_max_ms", "lexical_search_count", "lexical_search_total_ms", "lexical_search_max_ms", "dense_candidates", "lexical_candidates", "fused_candidates", "reranker_input_candidates", "reranked_candidates", "retrieved_candidates", "relevant_candidates", "grounding_candidates", "cited_sources", "top_score", "retrieval_query_length", "grounding_chunks", "grounding_message_count", "grounding_message_index", "context_chars", "grounding_context_chars", "citation_count", "temperature", "top_p", "top_k", "context_window", "context_used_tokens", "context_utilization", "prompt_tokens", "completion_tokens", "total_tokens", "ttft_ms", "thinking_duration_ms", "thinking_tokens", "thinking_budget", "generation_duration_ms", "total_duration_ms", "tokens_per_second"} and isinstance(value, (int, float)):
            output[key] = value
        elif key in {"context_truncated", "grounded_context_created", "grounding_applied", "physical_payload_grounding", "soul_applied", "query_expanded", "relevance_gate_applied"} and isinstance(value, bool):
            output[key] = value
        elif key == "fallback_errors" and isinstance(value, (list, tuple)):
            output[key] = [str(item) for item in value[:5] if isinstance(item, str)]
        elif key in {"selected_chunk_ids"} and isinstance(value, (list, tuple)):
            output[key] = [str(item) for item in value[:50] if isinstance(item, str)]
        elif key in {"top_scores"} and isinstance(value, (list, tuple)):
            output[key] = [float(item) for item in value[:50] if isinstance(item, (int, float))]
        elif key == "effective_tool_names" and isinstance(value, (list, tuple)):
            output[key] = [str(item) for item in value[:50] if isinstance(item, str)]
        elif key == "query_variants" and isinstance(value, (list, tuple)):
            output[key] = [str(item) for item in value[:5] if isinstance(item, str)]
        elif key == "candidate_scores" and isinstance(value, (list, tuple)):
            output[key] = [{name: item[name] for name in ("chunk_id", "dense_rank", "dense_score", "lexical_rank", "lexical_score", "rrf_rank", "rrf_score", "rerank_rank", "rerank_score", "final_rank", "relevant", "relevance_reason") if name in item and isinstance(item[name], (str, int, float, bool, type(None)))} for item in value[:50] if isinstance(item, Mapping)]
        elif key == "physical_message_roles" and isinstance(value, (list, tuple)):
            output[key] = [str(item) for item in value[:20] if isinstance(item, str)]
        elif key == "capabilities" and isinstance(value, (list, tuple)):
            output[key] = [str(item) for item in value[:20] if isinstance(item, (str, int, float))]
        elif key in {"confidence", "probabilities", "answers"} and isinstance(value, dict):
            # Decision shadow values are already structured operational observations.
            output[key] = {str(k): v for k, v in value.items() if isinstance(k, str)}
    return output


class RuntimeEventSink(Protocol):
    def start_event(self, kind: str, name: str, metadata: Mapping[str, Any] | None = None, parent_event_id: str | None = None) -> str | None: ...
    def finish_event(self, event_id: str | None, status: str, metadata: Mapping[str, Any] | None = None, duration_ms: float | None = None) -> None: ...


class NullRuntimeEventSink:
    def start_event(self, kind: str, name: str, metadata: Mapping[str, Any] | None = None, parent_event_id: str | None = None) -> None:
        return None

    def finish_event(self, event_id: str | None, status: str, metadata: Mapping[str, Any] | None = None, duration_ms: float | None = None) -> None:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
