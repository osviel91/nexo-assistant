from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class RerankerError(ValueError):
    pass


class Reranker(Protocol):
    provider_id: str
    model_id: str

    async def rerank(self, query: str, candidates: list[Any], limit: int) -> list[Any]: ...


@dataclass(frozen=True)
class OpenAICompatibleReranker:
    """Small provider adapter; retrieval only depends on the Reranker contract."""

    client: httpx.AsyncClient
    base_url: str
    headers: dict[str, str]
    provider_id: str
    model_id: str

    async def rerank(self, query: str, candidates: list[Any], limit: int) -> list[Any]:
        payload = {"model": self.model_id, "query": query, "documents": [item.text for item in candidates], "top_n": limit}
        response = await self.client.post(f"{self.base_url.rstrip('/')}/rerank", headers=self.headers, json=payload)
        response.raise_for_status()
        body = response.json()
        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            raise RerankerError("empty_result")
        ranked: list[Any] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise RerankerError("malformed_response")
            index = row.get("index")
            score = row.get("relevance_score", row.get("score"))
            if not isinstance(index, int) or index < 0 or index >= len(candidates) or not isinstance(score, (int, float)) or index in seen:
                raise RerankerError("malformed_response")
            seen.add(index)
            item = candidates[index]
            item.rerank_score = float(score)
            ranked.append(item)
        if len(ranked) != min(limit, len(candidates)):
            raise RerankerError("partial_result")
        return ranked


async def with_timeout(reranker: Reranker, query: str, candidates: list[Any], limit: int, timeout_ms: int) -> list[Any]:
    return await asyncio.wait_for(reranker.rerank(query, candidates, limit), timeout=max(timeout_ms, 1) / 1000)
