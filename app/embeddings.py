from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class EmbeddingError(Exception):
    pass


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    model: str | None = None


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> EmbeddingBatch: ...


def validate_batch(batch: EmbeddingBatch, expected: int) -> int:
    if len(batch.vectors) != expected:
        raise EmbeddingError("embedding response cardinality does not match request")
    dimension = 0
    for vector in batch.vectors:
        if not vector:
            raise EmbeddingError("embedding vector is empty")
        if dimension and len(vector) != dimension:
            raise EmbeddingError("embedding vectors have inconsistent dimensions")
        dimension = len(vector)
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise EmbeddingError("embedding vector contains a non-finite value")
    return dimension


class OpenAICompatibleEmbeddingProvider:
    def __init__(self, client: httpx.AsyncClient, base_url: str, headers: dict[str, str], model_id: str) -> None:
        self.client, self.base_url, self.headers, self.model_id = client, base_url.rstrip("/"), headers, model_id

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch([] , self.model_id)
        try:
            response = await self.client.post(f"{self.base_url}/embeddings", headers=self.headers,
                                              json={"model": self.model_id, "input": texts})
            if response.status_code >= 400:
                raise EmbeddingError(f"Embedding provider HTTP {response.status_code}")
            payload: dict[str, Any] = response.json()
            rows = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
            batch = EmbeddingBatch([row.get("embedding") for row in rows], payload.get("model", self.model_id))
            validate_batch(batch, len(texts))
            return batch
        except EmbeddingError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise EmbeddingError(f"Embedding provider failed: {str(exc)[:180]}") from exc
