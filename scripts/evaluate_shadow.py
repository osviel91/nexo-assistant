#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import argparse
import json
import os
import time
from pathlib import Path

from app.decision.models import ShadowDecision
from app.shadow_evaluation import (
    ADDITIONAL_DIAGNOSTIC_CASES,
    DIAGNOSTIC_CASES,
    DIAGNOSTIC_CASE_SETS,
    DIAGNOSTIC_MODES,
    DIAGNOSTIC_STATE_VARIANTS,
    WORDING_VARIANTS,
    diagnostic_request,
    evaluation_request,
    evaluation_runtime,
    latency_summary,
    score_cases,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the developer Shadow evaluation.")
    parser.add_argument("--model", choices=("laya-english", "laya-multilingual"), help="Evaluator-only Arbiter model override.")
    parser.add_argument("--wording", choices=tuple(WORDING_VARIANTS), default="baseline", help="Evaluator-only boolean wording variant.")
    parser.add_argument("--arbiter-url", default=os.getenv("NEXO_ARBITER_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("NEXO_ARBITER_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NEXO_DECISION_TIMEOUT", "10")))
    parser.add_argument("--tools", default="web_search", help="Stable comma-separated available tool names for every case.")
    parser.add_argument("--diagnostic-mode", choices=DIAGNOSTIC_MODES, help="Evaluator-only boolean isolation mode.")
    parser.add_argument("--state-variant", choices=DIAGNOSTIC_STATE_VARIANTS, help="Evaluator-only state construction.")
    parser.add_argument("--case-set", choices=DIAGNOSTIC_CASE_SETS, default="all", help="Evaluator-only diagnostic case set.")
    parser.add_argument("--output", default=None)
    return parser.parse_args()


async def run(options: argparse.Namespace | None = None) -> None:
    options = options or arguments()
    if bool(options.diagnostic_mode) != bool(options.state_variant):
        raise SystemExit("--diagnostic-mode and --state-variant must be supplied together")
    if options.diagnostic_mode and (not options.model or options.wording != "baseline"):
        raise SystemExit("diagnostics require --model and the baseline production wording")
    cases = json.loads((Path(__file__).parent.parent / "evaluation/shadow-cases.json").read_text())
    runtime = None
    service = None
    raw_responses = []
    if options.model:
        if not options.arbiter_url:
            raise SystemExit("--model requires --arbiter-url or NEXO_ARBITER_URL")
        runtime = evaluation_runtime(options.arbiter_url, options.api_key, options.timeout, options.model, raw_responses.append if options.diagnostic_mode else None)
    else:
        from app import main
        service = main.module_registry.service("decision-shadow")
        tools = sorted({tool["name"] for tool in main.module_registry.tool_catalog()})
    tools = sorted(item.strip() for item in options.tools.split(",") if item.strip()) if options.model else tools
    if options.diagnostic_mode:
        selected_cases = {
            "original": DIAGNOSTIC_CASES,
            "additional": ADDITIONAL_DIAGNOSTIC_CASES,
            "all": DIAGNOSTIC_CASES + ADDITIONAL_DIAGNOSTIC_CASES,
        }[options.case_set]
        cases = [{"id": case_id, "category": case_id, "state": message, "expected": {}} for case_id, message in selected_cases]
    results = []
    for case in cases:
        started = time.perf_counter()
        item = {"case_id": case["id"], "category": case["category"], "answers": {}, "model": None, "latency_ms": None, "error": None}
        try:
            if options.model:
                request = diagnostic_request(case["state"], tools, options.diagnostic_mode, options.state_variant) if options.diagnostic_mode else evaluation_request(case["state"], tools, options.wording)
                result = await runtime.decide(request)
                decision = ShadowDecision("evaluation", result.model, {key: value.model_dump(exclude_none=True) for key, value in result.answers.items()}, result.metadata or {}, round((time.perf_counter() - started) * 1000, 2))
            elif service is not None:
                decision = await service.shadow_decide(case["state"], tools)
            else:
                raise RuntimeError("decision_runtime_unavailable")
            item.update({"answers": decision.answers, "model": decision.model, "latency_ms": decision.latency_ms or round((time.perf_counter() - started) * 1000, 2), "error": decision.error})
            if options.diagnostic_mode:
                raw = raw_responses[-1]
                item.update({
                    "mode": options.diagnostic_mode,
                    "state_variant": options.state_variant,
                    "decisions": {
                        question_id: {
                            "raw_noul": raw.get("answers", {}).get(question_id, {}).get("noul"),
                            "normalized_boolean": answer.get("value"),
                            "confidence": answer.get("confidence"),
                            "model": decision.model,
                            "mode": options.diagnostic_mode,
                            "state_variant": options.state_variant,
                        }
                        for question_id, answer in decision.answers.items()
                        if question_id in {"needs_web", "needs_tools"}
                    },
                })
        except Exception as exc:
            item.update({"latency_ms": round((time.perf_counter() - started) * 1000, 2), "error": str(exc) if str(exc) == "decision_runtime_unavailable" else type(exc).__name__})
        results.append(item)
    if options.diagnostic_mode:
        report = {
            "model": options.model,
            "mode": options.diagnostic_mode,
            "state_variant": options.state_variant,
            "case_set": options.case_set,
            "wording": "baseline",
            "cases": len(cases),
            "results": results,
        }
    else:
        report = {"model": next((item["model"] for item in results if item["model"]), None), "wording": options.wording, "cases": len(cases), "results": results, "metrics": score_cases(cases, results), "latency_ms": latency_summary(results)}
    default_output = f"artifacts/decision-diagnostics-{options.diagnostic_mode}-{options.state_variant}.json" if options.diagnostic_mode else (f"artifacts/shadow-evaluation-{options.model}.json" if options.model else "artifacts/shadow-evaluation.json")
    output = Path(options.output) if options.output else Path(__file__).parent.parent / default_output
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    summary = {"cases": len(cases), "output": str(output)}
    if options.diagnostic_mode:
        summary.update({"mode": options.diagnostic_mode, "state_variant": options.state_variant})
    else:
        summary.update({"metrics": report["metrics"], "latency_ms": report["latency_ms"]})
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
