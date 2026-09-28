#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import argparse
import json
import os
import time
from pathlib import Path

from app.decision.models import ShadowDecision
from app.modules.decision_runtime import shadow_request
from app.shadow_evaluation import evaluation_runtime, latency_summary, score_cases


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the developer Shadow evaluation.")
    parser.add_argument("--model", choices=("laya-english", "laya-multilingual"), help="Evaluator-only Arbiter model override.")
    parser.add_argument("--arbiter-url", default=os.getenv("NEXO_ARBITER_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("NEXO_ARBITER_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NEXO_DECISION_TIMEOUT", "10")))
    parser.add_argument("--tools", default="web_search", help="Stable comma-separated available tool names for every case.")
    parser.add_argument("--output", default=None)
    return parser.parse_args()


async def run(options: argparse.Namespace | None = None) -> None:
    options = options or arguments()
    cases = json.loads((Path(__file__).parent.parent / "evaluation/shadow-cases.json").read_text())
    runtime = None
    service = None
    if options.model:
        if not options.arbiter_url:
            raise SystemExit("--model requires --arbiter-url or NEXO_ARBITER_URL")
        runtime = evaluation_runtime(options.arbiter_url, options.api_key, options.timeout, options.model)
    else:
        from app import main
        service = main.module_registry.service("decision-shadow")
        tools = sorted({tool["name"] for tool in main.module_registry.tool_catalog()})
    tools = sorted(item.strip() for item in options.tools.split(",") if item.strip()) if options.model else tools
    results = []
    for case in cases:
        started = time.perf_counter()
        item = {"case_id": case["id"], "category": case["category"], "answers": {}, "model": None, "latency_ms": None, "error": None}
        try:
            if options.model:
                result = await runtime.decide(shadow_request(case["state"], tools))
                decision = ShadowDecision("evaluation", result.model, {key: value.model_dump(exclude_none=True) for key, value in result.answers.items()}, result.metadata or {}, round((time.perf_counter() - started) * 1000, 2))
            elif service is not None:
                decision = await service.shadow_decide(case["state"], tools)
            else:
                raise RuntimeError("decision_runtime_unavailable")
            item.update({"answers": decision.answers, "model": decision.model, "latency_ms": decision.latency_ms or round((time.perf_counter() - started) * 1000, 2), "error": decision.error})
        except Exception as exc:
            item.update({"latency_ms": round((time.perf_counter() - started) * 1000, 2), "error": str(exc) if str(exc) == "decision_runtime_unavailable" else type(exc).__name__})
        results.append(item)
    report = {"model": next((item["model"] for item in results if item["model"]), None), "cases": len(cases), "results": results, "metrics": score_cases(cases, results), "latency_ms": latency_summary(results)}
    output = Path(options.output) if options.output else Path(__file__).parent.parent / (f"artifacts/shadow-evaluation-{options.model}.json" if options.model else "artifacts/shadow-evaluation.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"cases": len(cases), "metrics": report["metrics"], "latency_ms": report["latency_ms"]}, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
