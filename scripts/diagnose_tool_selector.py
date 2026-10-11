"""Measure ToolSelector wall time and the serialization it includes."""

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time

from app import tool_selection
from app.tool_selection import select_tools
from app.tools import EffectiveToolSet
from tests.test_tool_selection import tool


QUERY = "Busca información en mi vault"
SIZES = (10, 50, 100, 500)
WARMUPS = 10
SAMPLES = 100


def summary(values):
    ordered = sorted(values)
    return {
        "min_ms": round(ordered[0], 3),
        "median_ms": round(statistics.median(ordered), 3),
        "p05_ms": round(ordered[math.ceil(0.05 * len(ordered)) - 1], 3),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
        "current_test_formula_ms": round(ordered[int(0.95 * len(ordered)) - 1], 3),
        "max_ms": round(ordered[-1], 3),
        "stdev_ms": round(statistics.stdev(ordered), 3),
    }


def measure(size):
    start = time.perf_counter()
    catalog = EffectiveToolSet(tuple(
        tool(f"mcp.server.tool_{index}", "search read knowledge document")
        for index in range(size)
    ))
    construction_ms = (time.perf_counter() - start) * 1000

    original_schema_chars = tool_selection._schema_chars
    serialization_ms = []
    serialization_cpu_ms = []

    def timed_schema_chars(tools):
        started = time.perf_counter()
        before_cpu = time.process_time()
        result = original_schema_chars(tools)
        serialization_ms.append((time.perf_counter() - started) * 1000)
        serialization_cpu_ms.append((time.process_time() - before_cpu) * 1000)
        return result

    tool_selection._schema_chars = timed_schema_chars
    try:
        cold_before_cpu = time.process_time()
        started = time.perf_counter()
        cold = select_tools(catalog, QUERY, limit=10)
        cold_wall_ms = (time.perf_counter() - started) * 1000
        cold_cpu_ms = (time.process_time() - cold_before_cpu) * 1000
        cold_schema_ms = sum(serialization_ms[-2:])
        cold_schema_cpu_ms = sum(serialization_cpu_ms[-2:])

        for _ in range(WARMUPS):
            select_tools(catalog, QUERY, limit=10)

        wall_ms, cpu_ms, internal_ms, schema_ms, schema_cpu_ms = [], [], [], [], []
        for _ in range(SAMPLES):
            before_cpu = time.process_time()
            started = time.perf_counter()
            result = select_tools(catalog, QUERY, limit=10)
            wall_ms.append((time.perf_counter() - started) * 1000)
            cpu_ms.append((time.process_time() - before_cpu) * 1000)
            internal_ms.append(result.duration_ms)
            schema_ms.append(sum(serialization_ms[-2:]))
            schema_cpu_ms.append(sum(serialization_cpu_ms[-2:]))
    finally:
        tool_selection._schema_chars = original_schema_chars

    return {
        "size": size,
        "construction_ms_excluded_by_test": round(construction_ms, 3),
        "cold": {
            "wall_ms": round(cold_wall_ms, 3),
            "process_cpu_ms": round(cold_cpu_ms, 3),
            "internal_ms": cold.duration_ms,
            "serialization_ms": round(cold_schema_ms, 3),
            "serialization_process_cpu_ms": round(cold_schema_cpu_ms, 3),
        },
        "unwarmed_30": {
            "wall": summary(wall_ms[:30]),
            "process_cpu": summary(cpu_ms[:30]),
            "serialization": summary(schema_ms[:30]),
            "serialization_process_cpu": summary(schema_cpu_ms[:30]),
            "selector_process_cpu_minus_serialization": summary([
                total - schema for total, schema in zip(cpu_ms[:30], schema_cpu_ms[:30])
            ]),
        },
        "warmup_count": WARMUPS,
        "steady_100": {
            "wall": summary(wall_ms),
            "process_cpu": summary(cpu_ms),
            "selector_internal": summary(internal_ms),
            "serialization": summary(schema_ms),
            "serialization_process_cpu": summary(schema_cpu_ms),
            "selector_process_cpu_minus_serialization": summary([
                total - schema for total, schema in zip(cpu_ms, schema_cpu_ms)
            ]),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inject-cpu-regression", action="store_true")
    args = parser.parse_args()
    sizes = (500,) if args.inject_cpu_regression else SIZES
    original_select_tools = tool_selection.select_tools
    if args.inject_cpu_regression:
        def degraded_select_tools(*args, **kwargs):
            until = time.process_time() + 0.11
            while time.process_time() < until:
                pass
            return original_select_tools(*args, **kwargs)

        tool_selection.select_tools = degraded_select_tools
        globals()["select_tools"] = degraded_select_tools
    results = [measure(size) for size in sizes]
    print(json.dumps({
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "samples": SAMPLES,
        "warmups": WARMUPS,
        "percentile": "nearest rank; p95 index ceil(0.95*n)-1",
        "injected_cpu_regression": args.inject_cpu_regression,
        "results": results,
    }, indent=2))
    if args.inject_cpu_regression:
        result = results[0]["steady_100"]["process_cpu"]["p95_ms"]
        tool_selection.select_tools = original_select_tools
        if result < 100:
            raise SystemExit(f"Injected CPU regression was not detected: p95={result} ms")
        print(f"Detected injected CPU regression: p95={result} ms >= 100 ms")
