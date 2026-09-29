from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_REFERENCE_WORDS = re.compile(
    r"\b(eso|esa|ese|esto|este|esta|aquello|aquella|ellos|ellas|su|sus|cual|cuál|qué|que|it|that|this|they|them)\b",
    re.IGNORECASE,
)
_CLAUSE_SEPARATOR = re.compile(r"\s+(?:y|and|también|also)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class RetrievalPlan:
    original_query: str
    normalized_query: str
    variants: tuple[str, ...]
    standalone_query: str | None = None

    @property
    def expanded(self) -> bool:
        return len(self.variants) > 1


class QueryAnalyzer:
    """Small, deterministic query planner; no model call is needed for retrieval."""

    def __init__(self, max_variants: int = 3) -> None:
        self.max_variants = max(1, max_variants)

    def analyze(self, query: str, conversation: list[dict[str, Any]] | None = None) -> RetrievalPlan:
        original = query.strip()
        normalized = " ".join(original.split())
        variants: list[str] = [original]
        standalone: str | None = None

        if _REFERENCE_WORDS.search(original):
            previous = next(
                (str(message.get("content", "")).strip() for message in reversed(conversation or [])
                 if message.get("role") == "user" and str(message.get("content", "")).strip()),
                None,
            )
            if previous:
                standalone = f"{previous} {normalized}".strip()
                variants.append(standalone)

        clauses = [part.strip(" ?!.,;:") for part in _CLAUSE_SEPARATOR.split(normalized)]
        if len(clauses) > 1 and all(len(part.split()) >= 3 for part in clauses):
            variants.extend(clauses)
        if normalized != original:
            variants.append(normalized)

        unique = tuple(dict.fromkeys(item for item in variants if item))[: self.max_variants]
        return RetrievalPlan(original, normalized, unique, standalone)
