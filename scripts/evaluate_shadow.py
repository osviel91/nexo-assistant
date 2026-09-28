#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from app import main
from app.shadow_evaluation import latency_summary, score_cases


async def run() -> None:
    cases = json.loads((Path(__file__).parent.parent / "evaluation/shadow-cases.json").read_text())
    service = main.module_registry.service("decision-shadow")
    results = []
    for case in cases:
        started = time.perf_counter()
        item = {"case_id": case["id"], "category": case["category"], "answers": {}, "model": None, "latency_ms": None, "error": None}
        try:
            if service is None:
                raise RuntimeError("decision_runtime_unavailable")
            decision = await service.shadow_decide(case["state"], sorted({tool["name"] for tool in main.module_registry.tool_catalog()}))
            item.update({"answers": decision.answers, "model": decision.model, "latency_ms": decision.latency_ms or round((time.perf_counter() - started) * 1000, 2), "error": decision.error})
        except Exception as exc:
            item.update({"latency_ms": round((time.perf_counter() - started) * 1000, 2), "error": str(exc) if str(exc) == "decision_runtime_unavailable" else type(exc).__name__})
        results.append(item)
    report = {"model": next((item["model"] for item in results if item["model"]), None), "cases": len(cases), "results": results, "metrics": score_cases(cases, results), "latency_ms": latency_summary(results)}
    output = Path(__file__).parent.parent / "artifacts/shadow-evaluation.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"cases": len(cases), "metrics": report["metrics"], "latency_ms": report["latency_ms"]}, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
