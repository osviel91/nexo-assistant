from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Any

from app.decision.providers.arbiter import ArbiterDecisionProvider
from app.decision.runtime import DecisionRuntime


DIAGNOSTIC_CASES = [
    ("web-positive", "Busca en la web las noticias de hoy sobre OpenAI."),
    ("web-negative", "Explícame conceptualmente la diferencia entre TCP y UDP."),
    ("tool-positive", "Usa la herramienta de búsqueda web para consultar las noticias de hoy."),
]
DIAGNOSTIC_MODES = ("batch-current", "needs-web-only", "needs-tools-only")
DIAGNOSTIC_STATE_VARIANTS = ("production-state", "message-only")


WORDING_VARIANTS = {
    "baseline": {},
    "candidate-a": {
        "needs_web": "Would fulfilling the user's instruction require Nexo to retrieve information from the web because the user requests a search or the information is current or external?",
        "needs_tools": "Would fulfilling the user's instruction require invoking one of the capabilities Nexo exposes, rather than only generating an answer?",
    },
    "candidate-b": {
        "needs_web": "Is web retrieval necessary to provide the requested answer, including when the user explicitly asks to search the web or asks about information that may have changed?",
        "needs_tools": "Does completing the user's requested outcome require calling an available Nexo capability such as web_search, instead of answering from the model alone?",
    },
}


def evaluation_request(message: str, available_tools: list[str], wording: str):
    from app.modules.decision_runtime import shadow_request

    request = shadow_request(message, available_tools)
    if wording not in WORDING_VARIANTS:
        raise ValueError(f"unknown wording variant: {wording}")
    replacements = WORDING_VARIANTS[wording]
    for question in request.questions:
        if question.id in replacements:
            question.statement = replacements[question.id]
    return request


def evaluation_runtime(url: str, api_key: str, timeout: float, model: str, raw_response_sink=None) -> DecisionRuntime:
    """Build an evaluator-only runtime; production never calls this factory."""
    return DecisionRuntime(ArbiterDecisionProvider(url, api_key, timeout, model=model, raw_response_sink=raw_response_sink), timeout)


def diagnostic_request(message: str, available_tools: list[str], mode: str, state_variant: str):
    from app.modules.decision_runtime import shadow_request

    if mode not in DIAGNOSTIC_MODES:
        raise ValueError(f"unknown diagnostic mode: {mode}")
    if state_variant not in DIAGNOSTIC_STATE_VARIANTS:
        raise ValueError(f"unknown diagnostic state variant: {state_variant}")
    request = shadow_request(message, available_tools)
    if mode == "needs-web-only":
        request.questions = [question for question in request.questions if question.id == "needs_web"]
    elif mode == "needs-tools-only":
        request.questions = [question for question in request.questions if question.id == "needs_tools"]
    if state_variant == "message-only":
        request.state = {"message": message}
    return request


def score_cases(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {result["case_id"]: result for result in results}
    boolean = {field: {"correct": 0, "incorrect": 0, "false_positive": 0, "false_negative": 0} for field in ("needs_web", "needs_tools")}
    task = {"correct": 0, "incorrect": 0, "confusion": {}}
    confidence = {"correct": [], "incorrect": []}
    for case in cases:
        result = by_id.get(case["id"], {})
        answers = result.get("answers", {})
        expected = case.get("expected", {})
        for field in ("needs_web", "needs_tools"):
            if field not in expected or field not in answers:
                continue
            actual = answers[field].get("value")
            bucket = "correct" if actual == expected[field] else "incorrect"
            boolean[field][bucket] += 1
            if actual and not expected[field]: boolean[field]["false_positive"] += 1
            if not actual and expected[field]: boolean[field]["false_negative"] += 1
            confidence[bucket].append(answers[field].get("confidence", 0))
        if "task_type" in expected and "task_type" in answers:
            actual = str(answers["task_type"].get("value"))
            task["correct" if actual == expected["task_type"] else "incorrect"] += 1
            task["confusion"].setdefault(expected["task_type"], {})[actual] = task["confusion"].setdefault(expected["task_type"], {}).get(actual, 0) + 1
    for field in boolean:
        total = boolean[field]["correct"] + boolean[field]["incorrect"]
        boolean[field]["accuracy"] = round(boolean[field]["correct"] / total, 4) if total else None
    total = task["correct"] + task["incorrect"]
    task["accuracy"] = round(task["correct"] / total, 4) if total else None
    return {"boolean": boolean, "task_type": task, "confidence": {key: round(mean(values), 4) if values else None for key, values in confidence.items()}}


def latency_summary(results: list[dict[str, Any]]) -> dict[str, float | None]:
    values = sorted(float(item["latency_ms"]) for item in results if item.get("latency_ms") is not None)
    if not values:
        return {key: None for key in ("min", "p50", "p95", "max")}
    return {"min": values[0], "p50": values[(len(values) - 1) // 2], "p95": values[min(len(values) - 1, max(0, int(len(values) * .95) - 1))], "max": values[-1]}
