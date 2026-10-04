from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


RECOVERY_HINT = ("You are repeating tool work without obtaining significant new information. Review the results already collected. "
                 "Do not repeat equivalent calls. Use a materially different approach, answer with the available evidence, "
                 "or explain what information is missing.")
HARD_STOP_HINT = ("Further tool execution was stopped because repeated calls were not producing new information. "
                  "Answer using the evidence already collected and state any remaining uncertainty.")


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def call_fingerprint(tool: str, arguments: dict[str, Any]) -> str:
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    return _hash([tool, normalize(arguments)])


def result_fingerprint(projection: Any) -> str:
    # Bounded projection is supplied by ToolResultPipeline; hash only.
    return _hash(projection)


def _query_terms(arguments: dict[str, Any]) -> list[str]:
    raw = " ".join(str(value) for key, value in arguments.items() if key.casefold() in {"q", "query", "search", "term", "text", "filter"})
    return sorted({_hash(term) for term in re.findall(r"[\wáéíóúüñ]+", raw.casefold())[:32] if len(term) > 2})


def _evidence_ids(value: Any, key: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for name, item in value.items():
            if name.casefold() in {"id", "path", "uri", "source_id", "document_id", "resource_id", "chunk_id"} and isinstance(item, (str, int)):
                found.add(_hash(str(item)))
            else:
                found.update(_evidence_ids(item, name))
    elif isinstance(value, list):
        for item in value[:200]:
            found.update(_evidence_ids(item, key))
    return found


@dataclass
class ProgressGuard:
    enabled: bool = field(default_factory=lambda: os.getenv("NEXO_PROGRESS_GUARD", "true").strip().lower() not in {"0", "false", "off", "no"})
    recover_threshold: int = 3
    hard_stop_threshold: int = 6
    cycle_window: int = 8
    history: list[dict[str, Any]] = field(default_factory=list)
    score: int = 0
    recoveries: int = 0
    hard_stop: bool = False
    counters: Counter = field(default_factory=Counter)
    pending: dict[str, Any] | None = None

    @classmethod
    def restore(cls, state: dict[str, Any] | None, enabled: bool | None = None) -> "ProgressGuard":
        guard = cls()
        if isinstance(state, dict):
            guard.history = [item for item in state.get("history", []) if isinstance(item, dict)][-64:]
            guard.score = max(0, min(20, int(state.get("score", 0))))
            guard.recoveries = max(0, min(1, int(state.get("recoveries", 0))))
            guard.hard_stop = bool(state.get("hard_stop", False))
            guard.counters.update({k: int(v) for k, v in state.get("counters", {}).items() if isinstance(v, int) and v >= 0})
            pending = state.get("pending")
            if isinstance(pending, dict) and isinstance(pending.get("call"), str):
                guard.pending = {"call": pending["call"], "family": str(pending.get("family", ""))[:80],
                                 "query_terms": [term for term in pending.get("query_terms", []) if isinstance(term, str)][:32]}
        if enabled is not None:
            guard.enabled = enabled
        return guard

    def snapshot(self) -> dict[str, Any]:
        return {"history": self.history[-64:], "score": self.score, "recoveries": self.recoveries,
                "hard_stop": self.hard_stop, "counters": dict(self.counters), "pending": self.pending}

    def pending_call(self, identity: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        family = tool.rsplit(".", 1)[-1].casefold()
        family = "search" if any(word in family for word in ("search", "query", "find")) else family
        return {"call": identity, "family": family, "query_terms": _query_terms(arguments)}

    def observe_resumed_result(self, projection: Any, status: str) -> None:
        if self.pending:
            if status == "user_rejected":
                self.pending = None
                self.counters["approval_rejections"] += 1
                return
            pending = self.pending
            if any(item.get("call") == pending["call"] for item in self.history):
                self.counters["duplicate_calls"] += 1
                self.counters["exact_repeats"] += 1
                self.score = min(20, self.score + 2)
            self.counters["tool_calls"] += 1
            self.observe_result(projection, status, 0)

    def evaluate_pre_call(self, identity: str) -> str:
        if not self.enabled or self.hard_stop:
            return "allow"
        prior = sum(1 for event in self.history if event.get("call") == identity)
        # A retry after the sole recovery is the hard-stop boundary. The prior
        # call/result remains in native history; the proposal is not executed.
        if self.recoveries and prior >= 2:
            self.hard_stop = True
            self.counters["hard_stop_count"] += 1
            return "hard_stop"
        return "allow"

    def observe_call(self, identity: str, tool: str = "", arguments: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        self.pending = self.pending_call(identity, tool, arguments or {})
        self.counters["tool_calls"] += 1
        previous = [item for item in self.history if item.get("call") == identity]
        if previous:
            self.counters["duplicate_calls"] += 1
            self.counters["exact_repeats"] += 1
            self.score = min(20, self.score + 2)

    def observe_result(self, projection: Any, status: str, duration_ms: float, *, truncated: bool = False) -> dict[str, Any]:
        if not self.enabled or not self.pending:
            self.pending = None
            return {"action": "allow", "delta": 0}
        fingerprint = result_fingerprint(projection)
        neutral = {"ok", "success", "user_rejected", "approval_rejected", "approval_required", "waiting_approval"}
        safe_failures = {"invalid_arguments", "tool_not_available", "tool_execution_error", "repeated_approval_required",
                         "approval_unavailable", "approval_expired", "interaction_expired", "not_found", "timeout", "rate_limited"}
        failure = status if status in safe_failures else "tool_error" if status not in neutral else None
        prior_same = [item for item in self.history if item.get("result") == fingerprint]
        same_failure = [item for item in self.history if failure and item.get("failure") == failure and item.get("call") == self.pending["call"]]
        cycle = self._cycle(self.pending["call"], fingerprint)
        evidence = _evidence_ids(projection)
        seen = set().union(*(set(item.get("evidence", [])) for item in self.history)) if self.history else set()
        new_evidence = evidence - seen
        related_searches = [item for item in self.history[-8:] if item.get("family") == self.pending.get("family") == "search"]
        relatedness = max((len(set(item.get("query_terms", [])) & set(self.pending.get("query_terms", []))) /
                           max(1, len(set(item.get("query_terms", [])) | set(self.pending.get("query_terms", []))))
                           for item in related_searches), default=0)
        if evidence:
            self.counters["evidence_growth_events"] += bool(new_evidence)
            self.counters["evidence_resources_seen"] = len(seen | evidence)
        weak_growth = bool(related_searches and relatedness >= 0.5 and len(new_evidence) <= max(1, len(evidence) // 4))
        if weak_growth:
            self.score = min(20, self.score + 2)
            self.counters["low_progress_events"] += 1
        if truncated:
            self.counters["truncated_results"] += 1
            previous_truncated = any(item.get("truncated") and item.get("family") == self.pending.get("family") for item in self.history[-8:])
            if previous_truncated:
                self.score = min(20, self.score + 1)
                self.counters["repeated_truncation"] += 1
        if prior_same:
            self.counters["repeated_results"] += 1
            if not failure:
                self.score = min(20, self.score + 1)
        else:
            self.counters["unique_results"] += 1
            if not failure and not weak_growth:
                self.score = max(0, self.score - 2)
        if failure:
            self.counters["failed_calls"] += 1
        if same_failure:
            self.counters["repeated_failures"] += 1
            self.score = min(20, self.score + 2)
        if cycle:
            self.counters["cycles_detected"] += 1
            self.score = min(20, self.score + 2)
        self.history.append({**self.pending, "result": fingerprint, "failure": failure,
                             "evidence": sorted(evidence)[:200], "truncated": bool(truncated),
                             "duration_ms": max(0, round(duration_ms, 2))})
        self.history = self.history[-64:]
        self.counters["unique_calls"] = len({item["call"] for item in self.history})
        self.counters["total_tool_duration_ms"] = round(self.counters.get("total_tool_duration_ms", 0) + max(0, duration_ms), 2)
        self.pending = None
        action = "allow"
        if self.score >= self.hard_stop_threshold and self.recoveries:
            self.hard_stop = True
            self.counters["hard_stop_count"] += 1
            action = "hard_stop"
        elif self.score >= self.recover_threshold and self.recoveries == 0:
            self.recoveries = 1
            self.counters["recoveries"] = 1
            action = "recover"
        return {"action": action, "delta": self.score, "cycle": cycle,
                "repeated_result": bool(prior_same), "repeated_failure": bool(same_failure)}

    def _cycle(self, call: str, result: str) -> bool:
        tail = (self.history + [{"call": call, "result": result}])[-self.cycle_window:]
        keys = [(item.get("call"), item.get("result")) for item in tail]
        for period in (2, 3):
            if len(keys) >= period * 2 and keys[-period:] == keys[-2 * period:-period] and len(set(keys[-period:])) == period:
                if all(keys[-period + i][1] == keys[-2 * period + i][1] for i in range(period)):
                    return True
        return False
