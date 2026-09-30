from __future__ import annotations

import html
import json
import logging
import re
import time
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
    usage: dict[str, Any] | None = None
    provider_ttft_ms: float | None = None


class ModelAdapterError(Exception):
    pass


class _TextToolCallParser:
    OPEN = "<tool_call>"
    CLOSE = "</tool_call>"

    def __init__(self) -> None:
        self.buffer = ""
        self.ready = ""
        self.in_call = False
        self.index = 0

    def feed(self, text: str, final: bool = False) -> tuple[str, list[dict[str, Any]]]:
        self.buffer += text
        visible, calls = [], []
        while True:
            if not self.in_call:
                start = self.buffer.find(self.OPEN)
                if start < 0:
                    if final:
                        visible.append(self.ready + self.buffer)
                        self.ready = ""
                        self.buffer = ""
                    else:
                        keep = len(self.OPEN) - 1
                        if len(self.buffer) > keep:
                            self.ready += self.buffer[:-keep]
                            self.buffer = self.buffer[-keep:]
                            if len(self.ready) >= 32:
                                visible.append(self.ready)
                                self.ready = ""
                    break
                visible.append(self.ready + self.buffer[:start])
                self.ready = ""
                self.buffer = self.buffer[start + len(self.OPEN):]
                self.in_call = True
            end = self.buffer.find(self.CLOSE)
            if end < 0:
                if final:
                    self.buffer = ""
                    self.in_call = False
                break
            block, self.buffer = self.buffer[:end], self.buffer[end + len(self.CLOSE):]
            self.in_call = False
            match = re.search(r"<function=([^>\s]+)>(.*?)</function>", block, re.DOTALL)
            if not match:
                continue
            arguments = {}
            for parameter in re.finditer(r"<parameter=([^>\s]+)>(.*?)</parameter>", match.group(2), re.DOTALL):
                raw = html.unescape(parameter.group(2).strip())
                try:
                    arguments[parameter.group(1)] = json.loads(raw)
                except json.JSONDecodeError:
                    arguments[parameter.group(1)] = raw
            calls.append({"index": self.index, "id": f"text-tool-{self.index}", "type": "function", "function": {"name": match.group(1), "arguments": json.dumps(arguments, ensure_ascii=False)}})
            self.index += 1
        return "".join(visible), calls


class ModelAdapter(Protocol):
    model_id: str

    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], temperature: float | None = None) -> AsyncIterator[ModelStreamChunk]: ...


class OpenAICompatibleModelAdapter:
    def __init__(self, client: httpx.AsyncClient, url: str, headers: dict[str, str], model_id: str,
                 thinking: dict[str, Any] | None = None) -> None:
        self.client = client
        self.url = url
        self.headers = headers
        self.model_id = model_id
        self.thinking = thinking or {}
        self.reasoning_content = ""

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], temperature: float | None = None) -> AsyncIterator[ModelStreamChunk]:
        payload: dict[str, Any] = {"model": self.model_id, "messages": messages, "stream": True}
        if temperature is not None:
            payload["temperature"] = temperature
        if self.thinking.get("enabled") is not None and self.thinking.get("supported"):
            payload["thinking"] = {"type": "enabled" if self.thinking["enabled"] else "disabled"}
            if self.thinking.get("budget") is not None and self.thinking.get("budget_supported"):
                payload["thinking"]["budget"] = self.thinking["budget"]
        if tools:
            payload["tools"] = tools
        diagnostic(logger, "provider_request", **{
            "api_mode": "chat_completions",
            "endpoint": self.url,
            "tools_field_present": bool(tools),
            "tool_count": len(tools),
            "tool_names": [tool["function"]["name"] for tool in tools],
            "message_roles": [message.get("role") for message in messages],
            "message_count": len(messages),
            "grounding_message_indexes": [index for index, message in enumerate(messages) if "Retrieved Notebook material" in str(message.get("content", ""))],
            "grounding_message_chars": sum(len(str(message.get("content", ""))) for message in messages if "Retrieved Notebook material" in str(message.get("content", ""))),
        })
        request_started = time.perf_counter()
        first_content = True
        text_tool_parser = _TextToolCallParser()
        next_tool_index = 0
        pending_usage = None
        pending_finish_reason = None
        pending_ttft = None
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
                        packet = json.loads(raw)
                        choice = packet.get("choices", [{}])[0]
                        delta = choice.get("delta", {})
                        content = delta.get("content", "")
                        reasoning = delta.get("reasoning_content", delta.get("reasoning", ""))
                        if isinstance(reasoning, str) and self.thinking.get("reasoning_content"):
                            self.reasoning_content += reasoning
                        if isinstance(content, list):
                            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                        ttft = round((time.perf_counter() - request_started) * 1000, 2) if content and first_content else None
                        if ttft is not None:
                            first_content = False
                            pending_ttft = ttft
                        tool_calls = delta.get("tool_calls", []) or []
                        for call in tool_calls:
                            try:
                                next_tool_index = max(next_tool_index, int(call.get("index", 0)) + 1)
                            except (AttributeError, TypeError, ValueError):
                                continue
                        if isinstance(content, str):
                            content, fallback_calls = text_tool_parser.feed(content)
                            for call in fallback_calls:
                                call["index"] = next_tool_index
                                next_tool_index += 1
                            tool_calls = [*tool_calls, *fallback_calls]
                        usage = packet.get("usage")
                        finish_reason = choice.get("finish_reason")
                        if not content and not tool_calls and (usage or finish_reason):
                            pending_usage = usage or pending_usage
                            pending_finish_reason = finish_reason or pending_finish_reason
                            continue
                        chunk_ttft = pending_ttft if content else ttft
                        yield ModelStreamChunk(content=content, tool_calls=tool_calls, finish_reason=finish_reason or pending_finish_reason, usage=usage or pending_usage, provider_ttft_ms=chunk_ttft)
                        pending_usage = pending_finish_reason = None
                        if content:
                            pending_ttft = None
                    except (ValueError, IndexError, AttributeError, TypeError, json.JSONDecodeError):
                        continue
                content, fallback_calls = text_tool_parser.feed("", final=True)
                for call in fallback_calls:
                    call["index"] = next_tool_index
                    next_tool_index += 1
                if content or fallback_calls or pending_usage or pending_finish_reason:
                    yield ModelStreamChunk(content=content, tool_calls=fallback_calls, finish_reason=pending_finish_reason, usage=pending_usage, provider_ttft_ms=pending_ttft)
        except ModelAdapterError:
            raise
        except httpx.RequestError as exc:
            raise ModelAdapterError(f"Provider connection failed: {str(exc)[:180]}") from exc
