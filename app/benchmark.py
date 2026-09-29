from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
import statistics
import subprocess
import uuid
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


def parse_candidate_limits(value: str) -> list[int]:
    try:
        limits = [int(part.strip()) for part in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError("candidate limits must be comma-separated integers") from error
    if not limits or any(limit < 1 or limit > 200 for limit in limits) or len(set(limits)) != len(limits):
        raise argparse.ArgumentTypeError("candidate limits must be unique integers from 1 to 200")
    return limits


def live_run_valid(diagnostics: dict[str, Any], candidate_limit: int) -> bool:
    count = diagnostics.get("reranker_candidate_count")
    return (diagnostics.get("reranker_status") == "applied" and isinstance(count, int) and count > 0
            and count == min(candidate_limit, diagnostics.get("fused_candidate_count", 0))
            and diagnostics.get("reranked_candidate_count") == count)


def live_latency_statistics(cold: dict[str, Any], warm: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in warm if row["valid_for_candidate_comparison"]]
    return {"cold": cold.get("latency_ms"), "warm": stats([row["latency_ms"] for row in valid]),
            "invalid_warm_runs": len(warm) - len(valid)}


def _live_gold(chunks: list[Any], scenario: str) -> tuple[dict[str, list[Any]], list[str]]:
    anchors = {
        "direct_lexical": (("ley", "perversidad", "naturaleza"),),
        "semantic": (("tostada", "mantequilla"),),
        "conversational_follow_up": (("corolario",),),
        "ood": (),
        "compound": (("ley", "perversidad", "naturaleza"), ("corolario",)),
    }[scenario]
    matches = [row for row in chunks if any(all(word in row["content"].casefold() for word in group) for group in anchors)]
    return ({"chunk_ids": [row["id"] for row in matches], "source_ids": [], "page_numbers": []}, [row["source_id"] for row in matches])


async def _run_live(repetitions: int, candidate_limits: list[int], notebook_name: str, progress=None, include_git_commit=False) -> dict[str, Any]:
    from app import main
    import httpx

    config = main.embedding_configuration()
    if config is None or not config.reranking_enabled:
        raise RuntimeError("live benchmark requires configured embeddings and an enabled reranker")
    if os.getenv("NEXO_VECTOR_STORE", "local").strip().lower() != "qdrant":
        raise RuntimeError("live benchmark requires NEXO_VECTOR_STORE=qdrant")
    notebooks = [row for row in main.notebooks.list() if row["name"] == notebook_name]
    if len(notebooks) != 1:
        raise RuntimeError(f"live benchmark requires exactly one configured notebook named {notebook_name!r}")
    notebook_id = notebooks[0]["id"]
    with main.db() as connection:
        chunks = [dict(row) for row in connection.execute(
            "SELECT id,source_id,content FROM document_chunks WHERE notebook_id=?", (notebook_id,)
        ).fetchall()]
    gold_sets = {name: _live_gold(chunks, name) for name, *_ in SCENARIOS}
    output = []
    for candidate_limit in candidate_limits:
        rows = []
        for scenario_index, (name, query, conversation, _, expected_outcome) in enumerate(SCENARIOS):
            if progress:
                progress({"candidate_limit": candidate_limit, "scenario": name,
                          "completed": len(output) * len(SCENARIOS) + scenario_index,
                          "total": len(candidate_limits) * len(SCENARIOS)})
            async def measure():
                async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
                    override = replace(config, reranker_candidate_limit=candidate_limit)
                    service = main.retrieval_service(client, configuration_override=override)
                    if service.reranker is None or not isinstance(service.vector_store, main.QdrantVectorStore):
                        raise RuntimeError("live benchmark runtime did not construct configured Qdrant and reranker adapters")
                    if service.vector_store.health().status != "ok":
                        raise RuntimeError("configured Qdrant service is unavailable")
                    started = time.perf_counter()
                    results = await service.search(notebook_id, query, 5, conversation)
                    total = round((time.perf_counter() - started) * 1000, 2)
                    diagnostics = dict(service.last_diagnostics)
                    relevant = [row for row in results if row.get("relevant", True)]
                    context = GroundedContext.build(notebook_id, query, relevant, 4000) if relevant else None
                    outcome = (KnowledgeOutcome.GROUNDING_APPLIED.value if context and context.retrieval_results
                               else KnowledgeOutcome.NO_RELEVANT_EVIDENCE.value if results
                               else KnowledgeOutcome.NO_CANDIDATES.value)
                    source_ids = sorted({row.get("source_id") for row in (context.retrieval_results if context else []) if row.get("source_id")})
                    gold, expected_sources = gold_sets[name]
                    quality = {"recall_at_k": recall_at_k(results, gold, 5),
                               "precision_at_k": precision_at_k(results, gold, 5),
                               "mrr": mrr(results, gold), "ndcg_at_k": ndcg_at_k(results, gold, 5),
                               "gold_chunk_count": len(gold["chunk_ids"]), "status": "scenario_anchor_gold"}
                    if expected_outcome == KnowledgeOutcome.GROUNDING_APPLIED.value and not gold["chunk_ids"]:
                        quality = {key: None for key in ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k")}
                        quality.update({"gold_chunk_count": 0, "status": "gold_anchor_not_found"})
                    applied = live_run_valid(diagnostics, candidate_limit)
                    return {"latency_ms": total, "timings": diagnostics, "valid_for_candidate_comparison": bool(applied),
                            "invalid_reason": None if applied else diagnostics.get("reranker_reason", diagnostics.get("reranker_status", "reranker_not_applied")),
                            "reranker_status": diagnostics.get("reranker_status"),
                            "reranker_input_candidates": diagnostics.get("reranker_candidate_count"),
                            "reranked_candidates": diagnostics.get("reranked_candidate_count"),
                            "reranker_duration_ms": diagnostics.get("rerank_duration_ms"),
                            "reranker_provider": diagnostics.get("reranker_provider_id"),
                            "reranker_model": diagnostics.get("reranker_model"),
                            "quality": quality,
                            "expected_outcome": expected_outcome, "actual_outcome": outcome,
                            "expected_provenance": sorted(set(expected_sources)),
                            "actual_provenance": source_ids, "relevant_candidate_count": diagnostics.get("relevant_candidate_count", 0),
                            "relevant_candidate_ids": [row["chunk_id"] for row in results if row.get("relevant", True)],
                            "grounding_candidate_count": len(relevant), "citation_count": None,
                            "grounding_candidate_ids": [row["chunk_id"] for row in (context.retrieval_results if context else [])],
                            "citation_status": "retrieval_only_not_generated", "grounded_source_count": len(source_ids)}
            cold = await measure() if candidate_limit == 20 and scenario_index == 0 else {"status": "not_isolated", "latency_ms": None}
            warm_up = await measure()
            warm = [await measure() for _ in range(repetitions)]
            valid_warm = [row for row in warm if row["valid_for_candidate_comparison"]]
            observed = ([('cold', cold)] if cold.get("valid_for_candidate_comparison") is not None else [])
            observed.extend([("warm_up", warm_up), *[("warm", row) for row in warm]])
            invalid_runs = [{"phase": phase, "reason": row["invalid_reason"]}
                            for phase, row in observed
                            if not row["valid_for_candidate_comparison"]]
            rows.append({"name": name, "query_class": name, "execution": {"cold": cold, "warm_up": warm_up, "warm": warm},
                         "latency_statistics": {"reranker_duration_ms": stats([row["timings"].get("rerank_duration_ms") for row in valid_warm]),
                                                 "retrieval_total_ms": stats([row["timings"].get("retrieval_total_ms") for row in valid_warm]),
                                                 "retrieval_call_ms": live_latency_statistics(cold, warm)["warm"]},
                         "quality_statistics": {key: statistics.fmean(row["quality"][key] for row in valid_warm if row["quality"][key] is not None)
                                                if any(row["quality"][key] is not None for row in valid_warm) else None
                                                for key in ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k")},
                         "invalid_runs": invalid_runs})
        output.append({"candidate_limit": candidate_limit, "provider_id": config.reranker_provider_id,
                       "model_id": config.reranker_model, "scenarios": rows})
    return {"benchmark_version": "11B.1-1", "timestamp": datetime.now(timezone.utc).isoformat(),
             "git_commit": _git_commit() if include_git_commit else None, "mode": "live", "environment": {"vector_store": "qdrant", "reranker": "configured"},
            "configuration": {"candidate_limits": candidate_limits, "repetitions": repetitions, "warm_up": 1,
                               "notebook": notebook_name, "cold_scope": "first candidate-20 direct_lexical run only; remote provider state cannot be reset"},
             "experiments": output}


class RetrievalBenchmarkService:
    """Shared CLI/API entry to the Stage 11B.1 benchmark implementation."""

    MAX_CANDIDATE_LIMITS = 10
    MAX_REPETITIONS = 20

    def __init__(self):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.active_job: str | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def validate(cls, repetitions: int, candidate_limits: list[int]) -> None:
        if not 1 <= repetitions <= cls.MAX_REPETITIONS:
            raise ValueError(f"repetitions must be between 1 and {cls.MAX_REPETITIONS}")
        if not candidate_limits or len(candidate_limits) > cls.MAX_CANDIDATE_LIMITS:
            raise ValueError(f"candidate_limits must contain 1 to {cls.MAX_CANDIDATE_LIMITS} values")
        if any(not isinstance(value, int) or not 1 <= value <= 200 for value in candidate_limits) or len(set(candidate_limits)) != len(candidate_limits):
            raise ValueError("candidate limits must be unique integers from 1 to 200")

    async def run(self, repetitions: int, candidate_limits: list[int], notebook_name: str, include_git_commit=False) -> dict[str, Any]:
        self.validate(repetitions, candidate_limits)
        limits = [20, *[limit for limit in candidate_limits if limit != 20]]
        return await _run_live(repetitions, limits, notebook_name, include_git_commit=include_git_commit)

    async def start(self, repetitions: int, candidate_limits: list[int], notebook_name: str) -> dict[str, Any]:
        self.validate(repetitions, candidate_limits)
        async with self._lock:
            if self.active_job:
                raise RuntimeError("a retrieval benchmark is already running")
            job_id = uuid.uuid4().hex
            total = len([20, *[limit for limit in candidate_limits if limit != 20]]) * len(SCENARIOS)
            job = {"id": job_id, "status": "running", "progress": {"completed": 0,
                    "total": total}, "result": None, "error": None}
            self.jobs[job_id] = job
            self.active_job = job_id
            asyncio.create_task(self._execute(job, repetitions, candidate_limits, notebook_name))
            return self.public(job)

    async def _execute(self, job, repetitions, candidate_limits, notebook_name):
        def update(progress):
            job["progress"].update(progress)
        try:
            job["result"] = await _run_live(repetitions, [20, *[n for n in candidate_limits if n != 20]], notebook_name, update)
            job["progress"].update(completed=job["progress"]["total"], scenario=None, candidate_limit=None)
            job["status"] = "completed"
        except Exception as error:
            job["status"] = "failed"
            job["error"] = f"Benchmark failed ({type(error).__name__})"
        finally:
            async with self._lock:
                self.active_job = None
                while len(self.jobs) > 10:
                    oldest = next(iter(self.jobs))
                    if oldest == self.active_job:
                        break
                    self.jobs.pop(oldest)

    @staticmethod
    def public(job):
        return {key: job[key] for key in ("id", "status", "progress", "result", "error")}

    def get(self, job_id: str):
        job = self.jobs.get(job_id)
        return self.public(job) if job else None


def run_live(repetitions: int, candidate_limits: list[int], notebook_name: str) -> dict[str, Any]:
    return asyncio.run(RetrievalBenchmarkService().run(repetitions, candidate_limits, notebook_name, include_git_commit=True))


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before.get("experiments") and after.get("experiments"):
        baselines = {row["candidate_limit"]: row for row in before["experiments"]}
        baseline = baselines.get(20)
        if baseline is None:
            return {"error": "candidate-20 control is required"}
        output = {}
        baseline_scenarios = {row["name"]: row for row in baseline["scenarios"]}
        for experiment in after["experiments"]:
            scenario_deltas = {}
            for scenario in experiment["scenarios"]:
                old = baseline_scenarios.get(scenario["name"])
                if old is None:
                    continue
                old_latency = old["latency_statistics"]["reranker_duration_ms"]["p50"]
                new_latency = scenario["latency_statistics"]["reranker_duration_ms"]["p50"]
                scenario_deltas[scenario["name"]] = {
                    "reranker_p50_delta_ms": round(new_latency - old_latency, 2) if old_latency is not None and new_latency is not None else None,
                    "quality_delta": {key: round(scenario["quality_statistics"][key] - old["quality_statistics"][key], 4)
                                      if scenario["quality_statistics"][key] is not None and old["quality_statistics"][key] is not None else None
                                      for key in ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k")},
                    "invalid_runs": scenario["invalid_runs"],
                }
            output[str(experiment["candidate_limit"])] = scenario_deltas
        return output
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
    run.add_argument("--live", action="store_true", help="use configured provider, Qdrant, and reranker")
    run.add_argument("--reranker-candidates", type=parse_candidate_limits, default=None)
    run.add_argument("--notebook", default="Sabiduría de Murphy")
    run.add_argument("--repetitions", type=int, default=5)
    run.add_argument("--output", type=Path)
    cmp = sub.add_parser("compare")
    cmp.add_argument("before", type=Path)
    cmp.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    if args.command == "compare":
        print(json.dumps(compare(json.loads(args.before.read_text()), json.loads(args.after.read_text())), indent=2, sort_keys=True))
        return 0
    live = args.live or args.mode == "live"
    output = args.output or Path(f"artifacts/benchmarks/retrieval-{'live' if live else 'deterministic'}.json")
    if live:
        try:
            report = run_live(max(5, args.repetitions), args.reranker_candidates or [20], args.notebook)
        except Exception as error:
            parser.error(f"live benchmark unavailable: {error}")
        scenario_count = sum(len(experiment["scenarios"]) for experiment in report["experiments"])
    else:
        if args.reranker_candidates is not None:
            parser.error("--reranker-candidates requires --live")
        report = run_deterministic(max(1, args.repetitions))
        scenario_count = len(report["scenarios"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"mode": report["mode"], "output": str(output), "scenarios": scenario_count}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
