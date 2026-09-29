from __future__ import annotations

import time
import asyncio
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol

from app.chunking import ChunkingConfig, chunk_document
from app.embeddings import EmbeddingError, EmbeddingProvider, validate_batch
from app.vector_index import SQLiteVectorIndex, VectorIndex
from app.vector_store import VectorSearchFilters, VectorStore
from app.knowledge import EmbeddingConfiguration
from app.reranking import Reranker, RerankerError, with_timeout
from app.query_intelligence import QueryAnalyzer
from app.performance import RetrievalTelemetry


class RetrievalError(ValueError):
    pass


class IndexingInProgressError(RetrievalError):
    pass


@dataclass(frozen=True)
class RetrievalQuery:
    notebook_id: str
    text: str
    limit: int = 5
    mode: str = "hybrid"
    dense_limit: int = 20
    lexical_limit: int = 20
    rrf_k: int = 60


@dataclass
class RetrievalCandidate:
    chunk_id: str
    source_id: str
    document_id: str
    text: str
    provenance: list[dict]
    canonical_start: int
    canonical_end: int
    source_title: str | None = None
    document_content_hash: str | None = None
    chunk_content_hash: str | None = None
    ordinal: int = 0
    dense_rank: int | None = None
    dense_score: float | None = None
    lexical_rank: int | None = None
    lexical_score: float | None = None
    fused_score: float = 0.0
    rejection_reason: str | None = None
    final_rank: int | None = None
    rrf_rank: int | None = None
    rerank_score: float | None = None
    rerank_rank: int | None = None
    relevant: bool = True
    relevance_reason: str | None = None

    @classmethod
    def from_result(cls, result: Any) -> "RetrievalCandidate":
        return cls(result.chunk_id, result.source_id, result.document_id, result.content, result.provenance,
                   result.canonical_start, result.canonical_end, result.source_title,
                   result.document_content_hash, result.chunk_content_hash, result.ordinal)

    def as_dict(self) -> dict[str, Any]:
        return {"chunk_id": self.chunk_id, "source_id": self.source_id, "document_id": self.document_id,
                "score": round(self.fused_score, 6), "fused_score": round(self.fused_score, 6), "content": self.text,
                "canonical_start": self.canonical_start, "canonical_end": self.canonical_end,
                 "provenance": self.provenance, "document_content_hash": self.document_content_hash,
                 "chunk_content_hash": self.chunk_content_hash, "source_title": self.source_title,
                 "dense_rank": self.dense_rank, "dense_score": self.dense_score,
                 "lexical_rank": self.lexical_rank, "lexical_score": self.lexical_score,
                  "rrf_rank": self.rrf_rank, "rerank_score": self.rerank_score, "rerank_rank": self.rerank_rank,
                  "final_rank": self.final_rank, "accepted": self.rejection_reason is None and self.relevant,
                 "rejection_reason": self.rejection_reason, "relevant": self.relevant,
                 "relevance_reason": self.relevance_reason}


def _terms(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.lower()).encode("ascii", "ignore").decode()
    return {term for term in re.findall(r"[a-z0-9]{3,}", normalized) if term not in {
        "que", "cual", "cuales", "sobre", "dice", "documento", "para", "con", "una", "uno", "del", "las", "los", "the", "what", "about",
    }}


def apply_relevance_gate(candidates: list[RetrievalCandidate], query_variants: tuple[str, ...], min_overlap: int) -> int:
    query_terms = set().union(*(_terms(query) for query in query_variants))
    relevant_count = 0
    for candidate in candidates:
        overlap = len(query_terms & _terms(candidate.text))
        ranking_fallback = len(candidates) == 1 and candidate.final_rank == 1 and (candidate.dense_score is not None or candidate.lexical_score is not None)
        candidate.relevant = overlap >= min_overlap or ranking_fallback
        candidate.relevance_reason = "term_overlap" if overlap >= min_overlap else "ranking_fallback" if ranking_fallback else "insufficient_evidence"
        relevant_count += candidate.relevant
    return relevant_count


class Retriever(Protocol):
    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalCandidate]: ...


class DenseRetriever:
    def __init__(self, provider: EmbeddingProvider, index: VectorIndex, identity: dict | None = None,
                 vector_store: VectorStore | None = None) -> None:
        self.provider, self.index, self.identity, self.vector_store = provider, index, identity, vector_store

    async def retrieve(self, query: RetrievalQuery, telemetry: RetrievalTelemetry | None = None) -> list[RetrievalCandidate]:
        started = time.perf_counter()
        batch = await self.provider.embed([query.text])
        validate_batch(batch, 1)
        if telemetry is not None:
            telemetry.embedding_ms = round((time.perf_counter() - started) * 1000, 2) if telemetry.embedding_ms is None else round(telemetry.embedding_ms + (time.perf_counter() - started) * 1000, 2)
        started = time.perf_counter()
        if self.vector_store is not None:
            results = self.vector_store.search(batch.vectors[0], VectorSearchFilters(notebook_id=query.notebook_id, identity=self.identity), query.dense_limit)
        elif hasattr(self.index, "search_candidates"):
            results = self.index.search_candidates(query.notebook_id, batch.vectors[0], self.identity)
        else:
            results = self.index.search(query.notebook_id, batch.vectors[0], query.dense_limit, self.identity)
        if telemetry is not None:
            duration = RetrievalTelemetry.elapsed(started)
            telemetry.dense_search_ms = round((telemetry.dense_search_ms or 0) + duration, 2)
            telemetry.add_search("dense", duration)
        candidates = []
        for rank, result in enumerate(results[:query.dense_limit], 1):
            candidate = RetrievalCandidate.from_result(result)
            candidate.dense_rank, candidate.dense_score = rank, result.score
            candidates.append(candidate)
        return candidates


class LexicalRetriever:
    def __init__(self, index: VectorIndex, identity: dict | None = None) -> None:
        self.index, self.identity = index, identity

    async def retrieve(self, query: RetrievalQuery, telemetry: RetrievalTelemetry | None = None) -> list[RetrievalCandidate]:
        search = getattr(self.index, "lexical_search", None)
        if search is None:
            raise RetrievalError("lexical retrieval is unavailable")
        started = time.perf_counter()
        results = search(query.notebook_id, query.text, query.lexical_limit, self.identity) if self.identity is not None else search(query.notebook_id, query.text, query.lexical_limit)
        if telemetry is not None:
            duration = RetrievalTelemetry.elapsed(started)
            telemetry.lexical_search_ms = round((telemetry.lexical_search_ms or 0) + duration, 2)
            telemetry.add_search("lexical", duration)
        candidates = []
        for rank, result in enumerate(results, 1):
            candidate = RetrievalCandidate.from_result(result)
            candidate.lexical_rank, candidate.lexical_score = rank, result.score
            candidates.append(candidate)
        return candidates


class RankFusion(Protocol):
    def fuse(self, rankings: list[list[RetrievalCandidate]], k: int) -> list[RetrievalCandidate]: ...


class ReciprocalRankFusion:
    def fuse(self, rankings: list[list[RetrievalCandidate]], k: int = 60) -> list[RetrievalCandidate]:
        merged: dict[str, RetrievalCandidate] = {}
        for ranking in rankings:
            for rank, candidate in enumerate(ranking, 1):
                current = merged.setdefault(candidate.chunk_id, candidate)
                if candidate.dense_rank is not None:
                    current.dense_rank, current.dense_score = candidate.dense_rank, candidate.dense_score
                if candidate.lexical_rank is not None:
                    current.lexical_rank, current.lexical_score = candidate.lexical_rank, candidate.lexical_score
                current.fused_score += 1 / (k + rank)
        return sorted(merged.values(), key=lambda item: (-item.fused_score, item.chunk_id))


class MultiQueryFusion:
    """Fuse variant rankings without changing candidate identity or provenance."""

    def __init__(self, rrf: ReciprocalRankFusion | None = None) -> None:
        self.rrf = rrf or ReciprocalRankFusion()

    def fuse(self, rankings: list[list[RetrievalCandidate]], k: int = 60) -> list[RetrievalCandidate]:
        return self.rrf.fuse([ranking for ranking in rankings if ranking], k)


def _deduplicate(candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
    accepted: list[RetrievalCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.chunk_id in seen:
            continue
        overlap = next((prior for prior in accepted if prior.source_id == candidate.source_id and
                        min(prior.canonical_end, candidate.canonical_end) - max(prior.canonical_start, candidate.canonical_start) > 0 and
                        (min(prior.canonical_end, candidate.canonical_end) - max(prior.canonical_start, candidate.canonical_start)) /
                        max(1, min(prior.canonical_end - prior.canonical_start, candidate.canonical_end - candidate.canonical_start)) >= .8), None)
        if overlap:
            candidate.rejection_reason = "overlapping_chunk"
            continue
        seen.add(candidate.chunk_id)
        accepted.append(candidate)
    return accepted


class RetrievalService:
    def __init__(self, repository: Any, index: VectorIndex, provider: EmbeddingProvider,
                 config: ChunkingConfig | None = None, batch_size: int = 32,
                 embedding_config: EmbeddingConfiguration | None = None,
                 provider_id: str | None = None, model_id: str | None = None,
                 reranker: Reranker | None = None, vector_store: VectorStore | None = None,
                 query_analyzer: QueryAnalyzer | None = None) -> None:
        self.repository, self.index, self.provider = repository, index, provider
        self.config, self.batch_size = config or ChunkingConfig(), max(1, batch_size)
        self.embedding_config, self.provider_id, self.model_id = embedding_config, provider_id, model_id
        self.reranker = reranker
        self.vector_store = vector_store
        self.query_analyzer = query_analyzer or QueryAnalyzer()
        self.last_diagnostics: dict[str, Any] = {}
        self.last_telemetry = RetrievalTelemetry(
            vector_store="qdrant" if vector_store and vector_store.__class__.__name__ == "QdrantVectorStore" else "local"
        )

    async def index_source(self, notebook_id: str, source_id: str) -> dict[str, Any]:
        source = self.repository.source(notebook_id, source_id)
        if source is None:
            raise LookupError(source_id)
        document = self.repository.canonical(notebook_id, source_id)
        if document is None or source["status"] != "ready":
            raise RetrievalError("canonical document is not ready")
        if not self.repository.claim_indexing(source_id):
            raise IndexingInProgressError("indexing already in progress")
        with self.repository.connection_factory() as connection:
            existing_hash = connection.execute("SELECT metadata FROM document_chunks WHERE document_id=? LIMIT 1", (document["id"],)).fetchone()
            identity = connection.execute("SELECT * FROM vector_index_identities WHERE document_id=?", (document["id"],)).fetchone()
        current_identity = self._identity(document)
        reusable = bool(existing_hash and json.loads(existing_hash[0] or "{}").get("document_content_hash") == document["content_hash"])
        if self.embedding_config:
            reusable = reusable and identity is not None and all(
                identity[key] == value for key, value in current_identity.items() if key != "dimension"
            )
        if reusable:
            if self.vector_store is not None:
                if self.vector_store.count(VectorSearchFilters(document_id=document["id"])) != self._chunk_count(document["id"]):
                    self.vector_store.upsert(self.index.records(document["id"]))
            self.repository.set_indexing_status(source_id, "ready", chunk_count=self._chunk_count(document["id"]))
            return self.status(source_id) | {"skipped": True}
        started = time.monotonic()
        chunks = chunk_document(document, self.config)
        vectors: list[list[float]] = []
        batches = 0
        embedding_started = time.monotonic()
        try:
            for offset in range(0, len(chunks), self.batch_size):
                batch = await self.provider.embed([chunk.content for chunk in chunks[offset:offset + self.batch_size]])
                validate_batch(batch, len(chunks[offset:offset + self.batch_size]))
                vectors.extend(batch.vectors)
                batches += 1
            embedding_duration = round((time.monotonic() - embedding_started) * 1000, 2)
            if self.embedding_config and vectors:
                current_identity["dimension"] = len(vectors[0])
            indexed = self.index.upsert(chunks, vectors, current_identity if self.embedding_config else None)
            if self.vector_store is not None:
                self.vector_store.upsert([
                    record for record in self.index.records(document["id"])
                ])
            indexing_duration = round((time.monotonic() - started) * 1000, 2)
            self.repository.set_indexing_status(source_id, "ready", chunk_count=indexed, embedding_batches=batches,
                                                embedding_duration_ms=embedding_duration, indexing_duration_ms=indexing_duration,
                                                embedding_count=len(vectors), vector_count=indexed, embedding_dimension=len(vectors[0]) if vectors else None,
                                                embedding_config_version=self.embedding_config.config_version if self.embedding_config else None,
                                                indexing_identity=json.dumps(current_identity, sort_keys=True))
            return self.status(source_id) | {"skipped": False}
        except Exception as error:
            self.repository.set_indexing_status(source_id, "failed", error=str(error)[:240],
                                                embedding_batches=batches,
                                                embedding_duration_ms=round((time.monotonic() - embedding_started) * 1000, 2))
            raise EmbeddingError("indexing failed") from error

    async def search(self, notebook_id: str, query: str, limit: int = 5,
                     conversation: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if not query.strip():
            raise RetrievalError("query must not be empty")
        limit = min(max(limit, 1), 50)
        config = self.embedding_config
        mode = getattr(config, "retrieval_mode", "hybrid") if config else "hybrid"
        query_model = RetrievalQuery(notebook_id, query, limit, mode,
                                     getattr(config, "dense_candidate_limit", max(limit * 4, 20)),
                                     getattr(config, "lexical_candidate_limit", max(limit * 4, 20)),
                                     getattr(config, "rrf_k", 60))
        telemetry = RetrievalTelemetry(
            vector_store=self.last_telemetry.vector_store,
            retrieval_mode=mode,
            reranker_enabled=bool(getattr(config, "reranking_enabled", False)),
            reranker_provider=getattr(self.reranker, "provider_id", None),
            reranker_model=getattr(self.reranker, "model_id", None),
        )
        started = time.perf_counter()
        analysis_started = time.perf_counter()
        plan = self.query_analyzer.analyze(query, conversation)
        telemetry.query_analysis_ms = RetrievalTelemetry.elapsed(analysis_started)
        telemetry.query_variant_count = telemetry.query_variants = len(plan.variants)
        identity = self._identity({"content_hash": ""}) if config else None
        dense, lexical = [], []
        dense_rankings: list[list[RetrievalCandidate]] = []
        lexical_rankings: list[list[RetrievalCandidate]] = []
        failures: list[str] = []
        failure_reasons: dict[str, str] = {}
        dense_duration = lexical_duration = 0.0
        if mode in {"dense", "hybrid"}:
            phase_started = time.perf_counter()
            for variant in plan.variants:
                try:
                    ranking = await DenseRetriever(self.provider, self.index, identity, self.vector_store).retrieve(
                        RetrievalQuery(query_model.notebook_id, variant, query_model.limit, query_model.mode,
                                       query_model.dense_limit, query_model.lexical_limit, query_model.rrf_k), telemetry)
                    dense_rankings.append(ranking)
                except Exception as error:
                    if "dense" not in failures:
                        failures.append("dense")
                        failure_reasons["dense"] = "embedding_error" if isinstance(error, EmbeddingError) else "provider_error"
            dense_duration = RetrievalTelemetry.elapsed(phase_started)
            dense = [candidate for ranking in dense_rankings for candidate in ranking]
        if mode in {"lexical", "hybrid"}:
            phase_started = time.perf_counter()
            for variant in plan.variants:
                try:
                    ranking = await LexicalRetriever(self.index, identity).retrieve(
                        RetrievalQuery(query_model.notebook_id, variant, query_model.limit, query_model.mode,
                                       query_model.dense_limit, query_model.lexical_limit, query_model.rrf_k), telemetry)
                    lexical_rankings.append(ranking)
                except Exception as error:
                    if "lexical" not in failures:
                        failures.append("lexical")
                        failure_reasons["lexical"] = "unavailable" if isinstance(error, RetrievalError) else "index_error"
            lexical_duration = RetrievalTelemetry.elapsed(phase_started)
            lexical = [candidate for ranking in lexical_rankings for candidate in ranking]
        if not dense and not lexical and failures and len(failures) == (2 if mode == "hybrid" else 1):
            self.last_diagnostics = self._diagnostics(mode, "none", dense, lexical, failures, failure_reasons)
            if mode == "dense":
                raise RetrievalError("dense retrieval is unavailable")
        fusion_started = time.perf_counter()
        fused = MultiQueryFusion().fuse(dense_rankings + lexical_rankings, query_model.rrf_k)
        fusion_duration = RetrievalTelemetry.elapsed(fusion_started)
        for rank, candidate in enumerate(fused, 1):
            candidate.rrf_rank = rank
        results = _deduplicate(fused)
        effective_mode = "hybrid" if dense and lexical else "dense" if dense else "lexical" if lexical else "none"
        reranking_enabled = bool(getattr(config, "reranking_enabled", False))
        rerank_status, rerank_reason = "disabled", None
        rerank_count = reranked_count = 0
        rerank_duration = 0.0
        if reranking_enabled:
            rerank_count = min(getattr(config, "reranker_candidate_limit", 20), len(results))
            rerank_started = time.perf_counter()
            if self.reranker is None:
                rerank_status, rerank_reason = "fallback", "unavailable"
            else:
                try:
                    selected = results[:rerank_count]
                    ranked = await with_timeout(self.reranker, query, selected, rerank_count,
                                                getattr(config, "reranker_timeout_ms", 3000))
                    if len(ranked) != rerank_count or {item.chunk_id for item in ranked} != {item.chunk_id for item in selected}:
                        raise RerankerError("partial_result")
                    results = ranked + results[rerank_count:]
                    for rank, candidate in enumerate(ranked, 1):
                        candidate.rerank_rank = rank
                    reranked_count = len(ranked)
                    rerank_status = "applied"
                except asyncio.TimeoutError:
                    rerank_status, rerank_reason = "fallback", "timeout"
                except Exception as error:
                    reason = str(error) if isinstance(error, RerankerError) else "provider_error"
                    rerank_status, rerank_reason = "fallback", reason if reason in {"empty_result", "malformed_response", "partial_result"} else "provider_error"
            rerank_duration = RetrievalTelemetry.elapsed(rerank_started)
        for rank, candidate in enumerate(results[:limit], 1):
            candidate.final_rank = rank
        gate_enabled = bool(getattr(config, "relevance_gate_enabled", True))
        gate_min_overlap = int(getattr(config, "relevance_gate_min_term_overlap", 1))
        final_results = results[:limit]
        # Retrieval may use expanded conversation variants, but relevance must
        # be proven against the current user query, not inherited history.
        gate_started = time.perf_counter()
        relevant_count = apply_relevance_gate(final_results, (plan.original_query,), gate_min_overlap) if gate_enabled else len(final_results)
        relevance_gate_duration = RetrievalTelemetry.elapsed(gate_started)
        if not gate_enabled:
            for candidate in final_results:
                candidate.relevant, candidate.relevance_reason = True, "disabled"
        candidate_scores = [{"chunk_id": candidate.chunk_id, "dense_rank": candidate.dense_rank,
                             "dense_score": candidate.dense_score, "lexical_rank": candidate.lexical_rank,
                             "lexical_score": candidate.lexical_score, "rrf_rank": candidate.rrf_rank,
                             "rrf_score": candidate.fused_score, "rerank_rank": candidate.rerank_rank,
                             "rerank_score": candidate.rerank_score, "final_rank": candidate.final_rank,
                             "relevant": candidate.relevant, "relevance_reason": candidate.relevance_reason}
                            for candidate in final_results]
        telemetry.fusion_ms, telemetry.reranking_ms = fusion_duration, rerank_duration if reranking_enabled else None
        telemetry.relevance_gate_ms = relevance_gate_duration
        telemetry.retrieval_total_ms = RetrievalTelemetry.elapsed(started)
        telemetry.dense_candidates = len(dense)
        telemetry.lexical_candidates = len(lexical)
        telemetry.fused_candidates = len(fused)
        telemetry.reranker_input_candidates = rerank_count
        telemetry.reranked_candidates = reranked_count
        telemetry.retrieved_candidates = len(final_results)
        telemetry.relevant_candidates = relevant_count
        self.last_telemetry = telemetry
        self.last_diagnostics = self._diagnostics(mode, effective_mode, dense, lexical, failures, failure_reasons) | telemetry.as_dict() | {
                                   "fused_candidate_count": len(fused), "final_candidate_count": min(limit, len(results)),
                                   "query_variant_count": len(plan.variants), "query_expanded": plan.expanded,
                                   "query_variants": list(plan.variants),
                                   "dense_duration_ms": telemetry.dense_search_ms, "lexical_duration_ms": telemetry.lexical_search_ms,
                                   "fusion_duration_ms": fusion_duration,
                                  "reranking_enabled": reranking_enabled, "reranker_status": rerank_status,
                                  "reranker_reason": rerank_reason, "reranker_candidate_count": rerank_count,
                                   "reranked_candidate_count": reranked_count, "rerank_duration_ms": telemetry.reranking_ms,
                                   "reranker_provider_id": getattr(self.reranker, "provider_id", None) if reranking_enabled else None,
                                   "reranker_model": getattr(self.reranker, "model_id", None) if reranking_enabled else None,
                                   "retrieved_candidate_count": len(final_results),
                                   "relevance_gate_applied": gate_enabled,
                                   "relevant_candidate_count": relevant_count,
                                    "relevance_gate_status": "not_applied" if not gate_enabled else "accepted" if relevant_count else "rejected",
                                    "relevance_gate_reason": "disabled" if not gate_enabled else None if relevant_count else "insufficient_evidence",
                                   "relevance_gate_min_term_overlap": gate_min_overlap,
                                   "candidate_scores": candidate_scores,
                                   "retrieval_duration_ms": telemetry.retrieval_total_ms}
        return [candidate.as_dict() for candidate in final_results]

    def _diagnostics(self, configured_mode: str, effective_mode: str,
                     dense: list[RetrievalCandidate], lexical: list[RetrievalCandidate],
                     failures: list[str], failure_reasons: dict[str, str]) -> dict[str, Any]:
        fts_available = getattr(self.index, "fts_available", lambda: None)()
        return {"retrieval_mode": configured_mode, "configured_retrieval_mode": configured_mode,
                "effective_retrieval_mode": effective_mode, "dense_attempted": configured_mode in {"dense", "hybrid"},
                "dense_candidate_count": len(dense), "dense_failure_reason": failure_reasons.get("dense"),
                "lexical_attempted": configured_mode in {"lexical", "hybrid"}, "lexical_candidate_count": len(lexical),
                "lexical_failure_reason": failure_reasons.get("lexical"), "fts_available": fts_available,
                "fallback_errors": failures}

    async def inspect_search(self, notebook_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not query.strip():
            raise RetrievalError("query must not be empty")
        limit = min(max(limit, 1), 50)
        batch = await self.provider.embed([query])
        validate_batch(batch, 1)
        identity = self._identity({"content_hash": ""}) if self.embedding_config else None
        inspect = getattr(self.index, "inspect", None)
        if inspect is None:
            return []
        return inspect(notebook_id, batch.vectors[0], max(1000, limit), identity)

    def status(self, source_id: str) -> dict[str, Any]:
        with self.repository.connection_factory() as connection:
            row = connection.execute("""SELECT indexing_status,indexing_error,chunk_count,embedding_batches,
                embedding_duration_ms,indexing_duration_ms,indexed_at FROM notebook_sources WHERE id=?""", (source_id,)).fetchone()
        return dict(row) if row else {"indexing_status": "not_indexed"}

    def _identity(self, document: dict[str, Any]) -> dict[str, Any]:
        if not self.embedding_config:
            return {}
        return {"provider_id": self.provider_id, "model_id": self.model_id,
                "dimension": 0, "embedding_config_version": self.embedding_config.config_version,
                "chunking_config_hash": self.embedding_config.chunking_hash(), "document_hash": document["content_hash"]}

    def integrity(self, notebook_id: str, source_id: str) -> dict[str, Any]:
        document = self.repository.canonical(notebook_id, source_id)
        checks = []
        if not document:
            return {"status": "FAILED", "checks": [{"name": "DOCUMENT", "status": "FAIL", "detail": "canonical document missing"}]}
        with self.repository.connection_factory() as connection:
            identity = connection.execute("SELECT * FROM vector_index_identities WHERE document_id=?", (document["id"],)).fetchone()
            chunks = connection.execute("SELECT * FROM document_chunks WHERE document_id=? ORDER BY ordinal", (document["id"],)).fetchall()
            orphans = connection.execute("SELECT COUNT(*) FROM document_chunks dc LEFT JOIN canonical_documents cd ON cd.id=dc.document_id WHERE cd.id IS NULL").fetchone()[0]
        expected = len(chunks)
        checks.append({"name": "DOCUMENT_HASH", "status": "PASS" if identity and identity["document_hash"] == document["content_hash"] else "FAIL", "detail": document["content_hash"]})
        checks.append({"name": "CHUNK_COUNT", "status": "PASS", "detail": expected})
        vectors = [json.loads(row["embedding"]) for row in chunks if row["embedding"]]
        checks.append({"name": "VECTOR_COUNT", "status": "PASS" if len(vectors) == expected else "FAIL", "detail": f"{len(vectors)}/{expected}"})
        dimensions = {len(vector) for vector in vectors}
        dimension = identity["dimension"] if identity else None
        checks.append({"name": "DIMENSION", "status": "PASS" if identity and len(dimensions) == 1 and next(iter(dimensions), None) == dimension else "FAIL", "detail": dimension})
        finite = all(isinstance(value, (int, float)) and math.isfinite(value) for vector in vectors for value in vector)
        checks.append({"name": "FINITE_VALUES", "status": "PASS" if finite else "FAIL"})
        checks.append({"name": "ORPHANS", "status": "PASS" if not orphans else "FAIL", "detail": orphans})
        compatible = bool(identity and self.embedding_config and identity["provider_id"] == self.provider_id and identity["model_id"] == self.model_id and identity["embedding_config_version"] == self.embedding_config.config_version and identity["chunking_config_hash"] == self.embedding_config.chunking_hash())
        checks.append({"name": "EMBEDDING_IDENTITY", "status": "PASS" if compatible else "FAIL"})
        return {"status": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL", "checks": checks, "identity": dict(identity) if identity else None}

    def _chunk_count(self, document_id: str) -> int:
        with self.repository.connection_factory() as connection:
            return connection.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=?", (document_id,)).fetchone()[0]
