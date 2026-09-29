from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.embeddings import EmbeddingBatch
from app.grounding import GroundedContext, KnowledgeOutcome
from app.retrieval import RetrievalService
from app.retrieval_evaluation import mrr, ndcg_at_k, precision_at_k, recall_at_k
from app.vector_index import VectorSearchResult


class _Embedding:
    async def embed(self, texts):
        return EmbeddingBatch([[1.0, 0.0] for _ in texts], "benchmark-embedding")


class _Index:
    rows = (
        VectorSearchResult("law", "n", "murphy", "doc", 1.0, "LEY DE LA PERVERSIDAD DE LA NATURALEZA. La tostada cae del lado de la mantequilla.", 0, 100, []),
        VectorSearchResult("corollary", "n", "murphy", "doc", .9, "COROLARIO DE JENNING: la tostada cae siempre sobre el lado con mantequilla.", 101, 200, []),
        VectorSearchResult("kubernetes", "n", "other", "doc", .1, "Kubernetes aparece como ejemplo unrelated.", 201, 250, []),
    )

    def search_candidates(self, notebook_id, vector, identity):
        return list(self.rows)

    def lexical_search(self, notebook_id, query, limit, identity=None):
        terms = set(query.lower().split())
        return sorted(self.rows, key=lambda row: sum(term in row.content.lower() for term in terms), reverse=True)[:limit]


SCENARIOS = (
    ("direct_lexical", "¿Cuál es la ley de perversidad de la naturaleza?", None, {"chunk_ids": ["law"], "source_ids": [], "page_numbers": []}, KnowledgeOutcome.GROUNDING_APPLIED.value),
    ("semantic", "¿Por qué la tostada termina con la mantequilla hacia abajo?", None, {"chunk_ids": ["law", "corollary"], "source_ids": [], "page_numbers": []}, KnowledgeOutcome.GROUNDING_APPLIED.value),
    ("conversational_follow_up", "¿Y cuál es su corolario sobre la tostada?", [{"role": "user", "content": "¿Cuál es la ley de perversidad de la naturaleza?"}], {"chunk_ids": ["corollary"], "source_ids": [], "page_numbers": []}, KnowledgeOutcome.GROUNDING_APPLIED.value),
    ("ood", "¿Qué dice el documento sobre Kubernetes?", None, {"chunk_ids": [], "source_ids": [], "page_numbers": []}, KnowledgeOutcome.NO_RELEVANT_EVIDENCE.value),
    ("compound", "¿Cuál es la ley y cuál es su corolario sobre la tostada?", None, {"chunk_ids": ["law", "corollary"], "source_ids": [], "page_numbers": []}, KnowledgeOutcome.GROUNDING_APPLIED.value),
)


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {key: None for key in ("count", "min", "median", "p50", "p95", "max", "mean")}
    ordered = sorted(values)
    return {"count": len(values), "min": min(values), "median": statistics.median(values), "p50": statistics.median(values),
            "p95": ordered[max(0, int(len(ordered) * .95) - 1)], "max": max(values), "mean": statistics.fmean(values)}


async def _run_once(service: RetrievalService, query: str, conversation: list[dict[str, Any]] | None) -> dict[str, Any]:
    started = time.perf_counter()
    results = await service.search("n", query, 5, conversation)
    total_ms = round((time.perf_counter() - started) * 1000, 2)
    relevant = [item for item in results if item.get("relevant", True)]
    context = GroundedContext.build("n", query, relevant, 4000) if relevant else None
    outcome = KnowledgeOutcome.GROUNDING_APPLIED.value if context and context.retrieval_results else KnowledgeOutcome.NO_RELEVANT_EVIDENCE.value
    expected = next(item for item in SCENARIOS if item[1] == query)[3]
    quality = {"recall_at_k": recall_at_k(results, expected, 5), "precision_at_k": precision_at_k(results, expected, 5),
               "mrr": mrr(results, expected), "ndcg_at_k": ndcg_at_k(results, expected, 5)}
    diagnostics = dict(service.last_diagnostics)
    diagnostics["grounding_candidates"] = len(relevant)
    return {"latency_ms": total_ms, "timings": diagnostics, "candidate_counts": {key: diagnostics.get(key) for key in (
        "query_variant_count", "dense_candidate_count", "lexical_candidate_count", "fused_candidate_count", "reranker_candidate_count", "reranked_candidate_count", "retrieved_candidate_count", "relevant_candidate_count", "grounding_candidates")},
            "quality": quality, "expected_outcome": next(item for item in SCENARIOS if item[1] == query)[4], "actual_outcome": outcome,
            "expected_citation_provenance": "murphy" if expected["chunk_ids"] else None, "actual_citation_provenance": "murphy" if context else None}


def run_deterministic(repetitions: int = 5) -> dict[str, Any]:
    service = RetrievalService(object(), _Index(), _Embedding())
    scenarios = []
    for name, query, conversation, _, _ in SCENARIOS:
        cold = asyncio.run(_run_once(service, query, conversation))
        asyncio.run(_run_once(service, query, conversation))
        warm = [asyncio.run(_run_once(service, query, conversation)) for _ in range(repetitions)]
        scenarios.append({"name": name, "query_class": name, "execution": {"cold": cold, "warm_up": 1, "warm": warm},
                          "latency_statistics": {"total_request_ms": stats([row["latency_ms"] for row in warm]),
                                                  "retrieval_total_ms": stats([row["timings"].get("retrieval_total_ms", 0) for row in warm]),
                                                  "reranking_ms": stats([row["timings"].get("reranking_ms") for row in warm if row["timings"].get("reranking_ms") is not None])},
                          "quality_statistics": {key: statistics.fmean(row["quality"][key] for row in warm) for key in warm[0]["quality"]},
                          "outcomes": [{key: row[key] for key in ("expected_outcome", "actual_outcome", "expected_citation_provenance", "actual_citation_provenance")} for row in warm]})
    return {"benchmark_version": "11A-1", "timestamp": datetime.now(timezone.utc).isoformat(), "git_commit": _git_commit(),
            "mode": "deterministic", "environment": {"vector_store": "local", "embedding": "controlled", "reranker": "disabled"},
            "configuration": {"repetitions": repetitions, "warm_up": 1}, "scenarios": scenarios}


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    output = {}
    for old, new in zip(before.get("scenarios", []), after.get("scenarios", [])):
        name = new["name"]
        old_value = old["latency_statistics"]["total_request_ms"]["median"]
        new_value = new["latency_statistics"]["total_request_ms"]["median"]
        old_counts = old["execution"]["warm"][0]["candidate_counts"]
        new_counts = new["execution"]["warm"][0]["candidate_counts"]
        output[name] = {"latency_delta_ms": round(new_value - old_value, 2),
                        "latency_delta_percent": round((new_value - old_value) / old_value * 100, 2) if old_value else None,
                        "quality_delta": {key: round(new["quality_statistics"].get(key, 0) - old["quality_statistics"].get(key, 0), 4) for key in new["quality_statistics"]},
                        "candidate_count_delta": {key: new_counts.get(key, 0) - old_counts.get(key, 0) for key in new_counts}}
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=("deterministic", "live"), default="deterministic")
    run.add_argument("--repetitions", type=int, default=5)
    run.add_argument("--output", type=Path, default=Path("artifacts/benchmarks/retrieval-deterministic.json"))
    cmp = sub.add_parser("compare")
    cmp.add_argument("before", type=Path)
    cmp.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    if args.command == "compare":
        print(json.dumps(compare(json.loads(args.before.read_text()), json.loads(args.after.read_text())), indent=2, sort_keys=True))
        return 0
    if args.mode == "live":
        parser.error("live benchmark requires an explicit provider/Qdrant adapter and is not available in the deterministic harness")
    report = run_deterministic(max(1, args.repetitions))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"mode": report["mode"], "output": str(args.output), "scenarios": len(report["scenarios"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
