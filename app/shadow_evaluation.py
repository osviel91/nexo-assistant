from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Any


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
