from __future__ import annotations

import asyncio
import inspect
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class EvaluationCase:
    query: str
    notebook_id: str
    relevant: dict[str, list[Any]]


def _relevant(item: Any, truth: dict[str, list[Any]]) -> bool:
    candidate = item if isinstance(item, dict) else vars(item)
    return any(value is not None and value in truth.get(key, []) for key, value in (
        ("chunk_ids", candidate.get("chunk_id")), ("source_ids", candidate.get("source_id")),
        ("page_numbers", candidate.get("page_number", candidate.get("page"))),
    ))


def recall_at_k(results: list[Any], relevant: dict[str, list[Any]], k: int) -> float:
    if not relevant.get("chunk_ids") and not relevant.get("source_ids") and not relevant.get("page_numbers"):
        return 0.0
    total = len(relevant.get("chunk_ids") or relevant.get("source_ids") or relevant.get("page_numbers") or [])
    return min(total, sum(_relevant(item, relevant) for item in results[:k])) / total if total else 0.0


def precision_at_k(results: list[Any], relevant: dict[str, list[Any]], k: int) -> float:
    selected = results[:k]
    return sum(_relevant(item, relevant) for item in selected) / max(1, len(selected))


def mrr(results: list[Any], relevant: dict[str, list[Any]]) -> float:
    for index, item in enumerate(results, 1):
        if _relevant(item, relevant):
            return 1 / index
    return 0.0


def ndcg_at_k(results: list[Any], relevant: dict[str, list[Any]], k: int) -> float:
    gains = [1 if _relevant(item, relevant) else 0 for item in results[:k]]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = sum(1 / math.log2(index + 2) for index in range(min(k, len(relevant.get("chunk_ids") or relevant.get("source_ids") or relevant.get("page_numbers") or []))))
    return dcg / ideal if ideal else 0.0


@dataclass
class RetrievalEvaluationHarness:
    retrievers: dict[str, Callable[[str, str], Any]]
    cases: list[EvaluationCase] = field(default_factory=list)

    async def run(self, k: int = 5) -> dict[str, dict[str, Any]]:
        report: dict[str, dict[str, Any]] = {}
        for mode, retrieve in self.retrievers.items():
            rows = []
            for case in self.cases:
                started = time.monotonic()
                result = retrieve(case.notebook_id, case.query)
                result = await result if inspect.isawaitable(result) else result
                diagnostics = result.get("diagnostics", {}) if isinstance(result, dict) else {}
                results = result.get("results", []) if isinstance(result, dict) else result
                rows.append({"results": results, "diagnostics": diagnostics, "latency_ms": round((time.monotonic() - started) * 1000, 2), "case": case})
            report[mode] = {
                "recall_at_k": sum(recall_at_k(row["results"], row["case"].relevant, k) for row in rows) / max(1, len(rows)),
                "precision_at_k": sum(precision_at_k(row["results"], row["case"].relevant, k) for row in rows) / max(1, len(rows)),
                "mrr": sum(mrr(row["results"], row["case"].relevant) for row in rows) / max(1, len(rows)),
                "ndcg_at_k": sum(ndcg_at_k(row["results"], row["case"].relevant, k) for row in rows) / max(1, len(rows)),
                "candidate_count": sum(len(row["results"]) for row in rows),
                "latency_ms": round(sum(row["latency_ms"] for row in rows), 2),
                "retrieval_mode": mode,
                "reranking_latency_ms": round(sum(float(row["diagnostics"].get("rerank_duration_ms", 0) or 0) for row in rows), 2),
                "reranker_identity": next((f"{row['diagnostics'].get('reranker_provider_id')} / {row['diagnostics'].get('reranker_model')}" for row in rows if row["diagnostics"].get("reranker_provider_id")), None),
                "cases": len(rows),
            }
        return report


def run_evaluation(retrievers: dict[str, Callable[[str, str], Any]], cases: list[EvaluationCase], k: int = 5) -> dict[str, dict[str, Any]]:
    return asyncio.run(RetrievalEvaluationHarness(retrievers, cases).run(k))
