from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from app.diagnostics import diagnostic
from app.kernel import ModuleRegistry, ToolExecutionContext

logger = logging.getLogger("nexo.agent")


@dataclass(frozen=True)
class AgentRuntimeLimits:
    max_tool_rounds: int = 3
    max_tool_output_chars: int = 12000


class AgentRuntime:
    def __init__(self, registry: ModuleRegistry, limits: AgentRuntimeLimits | None = None) -> None:
        self.registry = registry
        self.limits = limits or AgentRuntimeLimits()

    async def stream(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        messages: list[dict[str, Any]],
        model_id: str,
        capabilities: set[str],
        context: ToolExecutionContext,
        temperature: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        answer = ""
        sources: list[dict[str, Any]] = []
        tools_used: list[str] = []
        tool_rounds = 0
        for _ in range(self.limits.max_tool_rounds + 1):
            payload = {"model": model_id, "messages": messages, "stream": True}
            if temperature is not None:
                payload["temperature"] = temperature
            definitions = self.registry.tool_definitions(context, capabilities)
            diagnostic(logger, "agent_runtime", **{
                "tools_available": len(self.registry.tool_catalog()),
                "tools_exposed": len(definitions),
                "exposed_tool_names": [tool["function"]["name"] for tool in definitions],
            })
            if tool_rounds < self.limits.max_tool_rounds and definitions:
                payload["tools"] = definitions
            tool_calls: dict[int, dict[str, Any]] = {}
            round_content = ""
            finish_reason = None
            try:
                diagnostic(logger, "provider_request", **{
                    "api_mode": "chat_completions",
                    "endpoint": url,
                    "tools_field_present": "tools" in payload,
                    "tool_count": len(payload.get("tools", [])),
                    "tool_names": [tool["function"]["name"] for tool in payload.get("tools", [])],
                    "tool_choice": payload.get("tool_choice"),
                })
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        diagnostic(logger, "provider_response", status=response.status_code, finish_reason=None, tool_calls_present=False, tool_call_names=[])
                        yield {"error": f"Provider HTTP {response.status_code}"}
                        return
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            choice = json.loads(raw).get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            finish_reason = choice.get("finish_reason") or finish_reason
                            text = delta.get("content", "")
                            if isinstance(text, list):
                                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
                            if text:
                                round_content += text
                                answer += text
                                yield {"delta": text}
                            for call in delta.get("tool_calls", []) or []:
                                index = int(call.get("index", 0))
                                current = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                                current["id"] += call.get("id", "") or ""
                                function = call.get("function", {})
                                current["name"] += function.get("name", "") or ""
                                current["arguments"] += function.get("arguments", "") or ""
                        except (ValueError, IndexError, AttributeError, TypeError, json.JSONDecodeError):
                            continue
            except httpx.RequestError as exc:
                yield {"error": f"Provider connection failed: {str(exc)[:180]}"}
                return

            diagnostic(logger, "provider_response", **{
                "status": 200,
                "finish_reason": finish_reason,
                "tool_calls_present": bool(tool_calls),
                "tool_call_names": [call["name"] for call in tool_calls.values() if call["name"]],
            })

            if not tool_calls:
                diagnostic(logger, "agent_loop", tool_rounds=tool_rounds, executed_tool_names=tools_used)
                yield {"complete": True, "answer": answer, "sources": sources, "tools_used": tools_used, "tool_rounds": tool_rounds}
                return
            if tool_rounds >= self.limits.max_tool_rounds:
                yield {"error": "Se alcanzó el límite de rondas de herramientas."}
                return

            messages.append({
                "role": "assistant",
                "content": round_content or None,
                "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in tool_calls.values()],
            })
            tool_rounds += 1
            for call in tool_calls.values():
                tools_used.append(call["name"])
                tool_context = ToolExecutionContext(context.conversation_id, context.provider_id, context.model_id, tool_rounds)
                started = time.monotonic()
                status = "ok"
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError
                except (ValueError, TypeError, json.JSONDecodeError):
                    result = {"error": {"code": "invalid_arguments", "message": "Argumentos de herramienta inválidos."}}
                    status = "invalid_arguments"
                else:
                    try:
                        result = await self.registry.invoke_tool(call["name"], tool_context, arguments)
                    except KeyError:
                        result = {"error": {"code": "unknown_tool", "message": "Herramienta desconocida."}}
                        status = "unknown_tool"
                    except Exception:
                        result = {"error": {"code": "tool_execution_error", "message": "La herramienta no pudo completar la operación."}}
                        status = "tool_execution_error"
                        logger.exception("tool execution failed", extra={"conversation_id": context.conversation_id, "provider_id": context.provider_id, "model_id": context.model_id, "tool": call["name"], "round": tool_rounds})
                if not isinstance(result, dict):
                    result = {"error": {"code": "tool_execution_error", "message": "La herramienta devolvió un resultado inválido."}}
                    status = "tool_execution_error"
                if isinstance(result.get("results"), list):
                    numbered = []
                    for item in result["results"]:
                        if not isinstance(item, dict):
                            continue
                        item = dict(item)
                        item["source"] = len(sources) + 1
                        sources.append({"title": item.get("title", "Fuente"), "url": item.get("url", "")})
                        numbered.append(item)
                    result["results"] = numbered
                result_text = self._serialize_tool_result(result)
                if result.get("error"):
                    yield {"status": "tool_error", "tool": call["name"], "message": result["error"].get("message", "Error de herramienta")}
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": result_text})
                logger.info("tool call", extra={"conversation_id": context.conversation_id, "provider_id": context.provider_id, "model_id": context.model_id, "tool": call["name"], "round": tool_rounds, "status": status, "duration": round(time.monotonic() - started, 4)})
            diagnostic(logger, "agent_loop", tool_rounds=tool_rounds, executed_tool_names=tools_used)
        yield {"error": "Se alcanzó el límite de rondas de herramientas."}

    def _serialize_tool_result(self, result: dict[str, Any]) -> str:
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(raw) <= self.limits.max_tool_output_chars:
            return raw
        marker = {"error": {"code": "tool_output_truncated", "message": "Tool output truncated."}, "truncated": True, "output": ""}
        if len(json.dumps(marker, ensure_ascii=False, separators=(",", ":"))) > self.limits.max_tool_output_chars:
            return json.dumps({"error": {"code": "tool_output_truncated"}, "truncated": True}, separators=(",", ":"))
        while True:
            marker["output"] = raw[: max(0, self.limits.max_tool_output_chars - len(json.dumps(marker, ensure_ascii=False, separators=(",", ":"))))]
            serialized = json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
            if len(serialized) <= self.limits.max_tool_output_chars or not marker["output"]:
                return serialized
