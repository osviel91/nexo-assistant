from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.diagnostics import diagnostic


logger = logging.getLogger("nexo.agent.model")


@dataclass(frozen=True)
class ModelStreamChunk:
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None


class ModelAdapterError(Exception):
    pass


class ModelAdapter(Protocol):
    model_id: str

    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], temperature: float | None = None) -> AsyncIterator[ModelStreamChunk]: ...


class OpenAICompatibleModelAdapter:
    def __init__(self, client: httpx.AsyncClient, url: str, headers: dict[str, str], model_id: str) -> None:
        self.client = client
        self.url = url
        self.headers = headers
        self.model_id = model_id

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], temperature: float | None = None) -> AsyncIterator[ModelStreamChunk]:
        payload: dict[str, Any] = {"model": self.model_id, "messages": messages, "stream": True}
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
        diagnostic(logger, "provider_request", **{
            "api_mode": "chat_completions",
            "endpoint": self.url,
            "tools_field_present": bool(tools),
            "tool_count": len(tools),
            "tool_names": [tool["function"]["name"] for tool in tools],
        })
        try:
            async with self.client.stream("POST", self.url, headers=self.headers, json=payload) as response:
                if response.status_code >= 400:
                    diagnostic(logger, "provider_response", status=response.status_code, finish_reason=None, tool_calls_present=False, tool_call_names=[])
                    raise ModelAdapterError(f"Provider HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        choice = json.loads(raw).get("choices", [{}])[0]
                        delta = choice.get("delta", {})
                        content = delta.get("content", "")
                        if isinstance(content, list):
                            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                        tool_calls = delta.get("tool_calls", []) or []
                        yield ModelStreamChunk(content=content, tool_calls=tool_calls, finish_reason=choice.get("finish_reason"))
                    except (ValueError, IndexError, AttributeError, TypeError, json.JSONDecodeError):
                        continue
        except ModelAdapterError:
            raise
        except httpx.RequestError as exc:
            raise ModelAdapterError(f"Provider connection failed: {str(exc)[:180]}") from exc
