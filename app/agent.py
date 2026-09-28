from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from app.agent_model import ModelAdapter, ModelAdapterError
from app.diagnostics import diagnostic
from app.kernel import ToolExecutionContext
from app.tools import EffectiveToolSet, ToolExecutor, ToolNotAvailableError

logger = logging.getLogger("nexo.agent")


@dataclass(frozen=True)
class AgentRuntimeLimits:
    max_tool_rounds: int = 3
    max_tool_output_chars: int = 12000


@dataclass(frozen=True)
class AgentRunRequest:
    model: ModelAdapter
    messages: list[dict[str, Any]]
    effective_tools: EffectiveToolSet
    tool_executor: ToolExecutor
    context: ToolExecutionContext
    temperature: float | None = None


class AgentRuntime:
    def __init__(self, limits: AgentRuntimeLimits | None = None) -> None:
        self.limits = limits or AgentRuntimeLimits()

    async def stream(
        self,
        request: AgentRunRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        answer = ""
        sources: list[dict[str, Any]] = []
        tools_used: list[str] = []
        tool_rounds = 0
        for _ in range(self.limits.max_tool_rounds + 1):
            reason_started = time.perf_counter()
            definitions = request.effective_tools.definitions()
            diagnostic(logger, "agent_runtime", **{
                "tools_available": len(definitions),
                "tools_exposed": len(definitions),
                "exposed_tool_names": [tool["function"]["name"] for tool in definitions],
            })
            tool_calls: dict[int, dict[str, Any]] = {}
            round_content = ""
            finish_reason = None
            try:
                async for chunk in request.model.stream(request.messages, definitions if tool_rounds < self.limits.max_tool_rounds else [], request.temperature):
                    finish_reason = chunk.finish_reason or finish_reason
                    if chunk.content:
                        round_content += chunk.content
                        answer += chunk.content
                        yield {"delta": chunk.content}
                    for call in chunk.tool_calls or []:
                        index = int(call.get("index", 0))
                        current = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        current["id"] += call.get("id", "") or ""
                        function = call.get("function", {})
                        current["name"] += function.get("name", "") or ""
                        current["arguments"] += function.get("arguments", "") or ""
            except ModelAdapterError as exc:
                yield {"trace": {"type": "REASON", "duration_ms": round((time.perf_counter() - reason_started) * 1000, 2), "status": "error", "metadata": {"model": request.model.model_id, "round": tool_rounds + 1, "run_id": request.context.run_id}}}
                yield {"error": str(exc)}
                return

            diagnostic(logger, "provider_response", **{
                "status": 200,
                "finish_reason": finish_reason,
                "tool_calls_present": bool(tool_calls),
                "tool_call_names": [call["name"] for call in tool_calls.values() if call["name"]],
            })
            yield {"trace": {"type": "REASON", "duration_ms": round((time.perf_counter() - reason_started) * 1000, 2), "status": "success", "metadata": {"model": request.model.model_id, "round": tool_rounds + 1, "run_id": request.context.run_id}}}

            if not tool_calls:
                diagnostic(logger, "agent_loop", tool_rounds=tool_rounds, executed_tool_names=tools_used)
                yield {"complete": True, "answer": answer, "sources": sources, "tools_used": tools_used, "tool_rounds": tool_rounds}
                return
            if tool_rounds >= self.limits.max_tool_rounds:
                yield {"error": "Se alcanzó el límite de rondas de herramientas."}
                return

            request.messages.append({
                "role": "assistant",
                "content": round_content or None,
                "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in tool_calls.values()],
            })
            tool_rounds += 1
            for call in tool_calls.values():
                tools_used.append(call["name"])
                tool_context = ToolExecutionContext(request.context.conversation_id, request.context.provider_id, request.context.model_id, tool_rounds, request.context.run_id)
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
                        result = await request.tool_executor.invoke(request.effective_tools, call["name"], tool_context, arguments)
                    except ToolNotAvailableError:
                        result = {"error": {"code": "tool_not_available", "message": "La herramienta no está disponible para esta ejecución."}}
                        status = "tool_not_available"
                    except Exception:
                        result = {"error": {"code": "tool_execution_error", "message": "La herramienta no pudo completar la operación."}}
                        status = "tool_execution_error"
                        logger.exception("tool execution failed", extra={"conversation_id": request.context.conversation_id, "provider_id": request.context.provider_id, "model_id": request.context.model_id, "tool": call["name"], "round": tool_rounds})
                if not isinstance(result, dict):
                    result = {"error": {"code": "tool_execution_error", "message": "La herramienta devolvió un resultado inválido."}}
                    status = "tool_execution_error"
                yield {"trace": {"type": "ACT", "duration_ms": round(time.monotonic() - started, 4) * 1000, "status": "success" if status == "ok" and not result.get("error") else status, "metadata": {"tool": call["name"], "round": tool_rounds, "run_id": request.context.run_id}}}
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
                request.messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": result_text})
                logger.info("tool call", extra={"conversation_id": request.context.conversation_id, "provider_id": request.context.provider_id, "model_id": request.context.model_id, "tool": call["name"], "round": tool_rounds, "status": status, "duration": round(time.monotonic() - started, 4)})
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
