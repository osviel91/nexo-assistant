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
from app.runtime_trace import NullRuntimeEventSink, RuntimeEventSink
from app.grounding import GROUNDING_INSTRUCTIONS, GroundedContext

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
    event_sink: RuntimeEventSink = NullRuntimeEventSink()
    system_instructions: str = ""
    profile_id: str | None = None
    profile_name: str | None = None
    runtime_snapshot: dict[str, Any] | None = None
    grounded_context: GroundedContext | None = None


class AgentRuntime:
    def __init__(self, limits: AgentRuntimeLimits | None = None) -> None:
        self.limits = limits or AgentRuntimeLimits()

    async def stream(
        self,
        request: AgentRunRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        messages = list(request.messages)
        if request.system_instructions:
            messages.insert(0, {"role": "system", "content": request.system_instructions})
        if request.grounded_context is not None:
            messages.insert(1 if request.system_instructions else 0, {"role": "system", "content": GROUNDING_INSTRUCTIONS})
            messages.insert(2 if request.system_instructions else 1, {"role": "system", "content": request.grounded_context.serialize()})
        answer = ""
        sources: list[dict[str, Any]] = []
        tools_used: list[str] = []
        tool_rounds = 0
        request_started = time.perf_counter()
        first_content_at: float | None = None
        usage: dict[str, int] = {}
        for _ in range(self.limits.max_tool_rounds + 1):
            reason_started = time.perf_counter()
            reason_metadata = {"model": request.model.model_id, "round": tool_rounds + 1,
                               **(request.runtime_snapshot or {}),
                               **({"agent_profile_id": request.profile_id} if request.profile_id else {})}
            reason_event_id = request.event_sink.start_event("REASON", request.model.model_id, reason_metadata)
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
                async for chunk in request.model.stream(messages, definitions if tool_rounds < self.limits.max_tool_rounds else [], request.temperature):
                    finish_reason = chunk.finish_reason or finish_reason
                    if chunk.usage:
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            if isinstance(chunk.usage.get(key), int):
                                usage[key] = usage.get(key, 0) + chunk.usage[key]
                    if chunk.content:
                        first_content_at = first_content_at or time.perf_counter()
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
                duration_ms = round((time.perf_counter() - reason_started) * 1000, 2)
                request.event_sink.finish_event(reason_event_id, "failed", {**reason_metadata, "error_code": "provider_failure"}, duration_ms)
                yield {"trace": {"type": "REASON", "duration_ms": duration_ms, "status": "error", "metadata": {**reason_metadata, "run_id": request.context.run_id}}}
                yield {"error": "provider_failure" if request.profile_id else str(exc)}
                return

            diagnostic(logger, "provider_response", **{
                "status": 200,
                "finish_reason": finish_reason,
                "tool_calls_present": bool(tool_calls),
                "tool_call_names": [call["name"] for call in tool_calls.values() if call["name"]],
            })
            duration_ms = round((time.perf_counter() - reason_started) * 1000, 2)
            request.event_sink.finish_event(reason_event_id, "completed", reason_metadata, duration_ms)
            yield {"trace": {"type": "REASON", "duration_ms": duration_ms, "status": "success", "metadata": {**reason_metadata, "run_id": request.context.run_id}}}

            if not tool_calls:
                diagnostic(logger, "agent_loop", tool_rounds=tool_rounds, executed_tool_names=tools_used)
                completed_at = time.perf_counter()
                telemetry = {**usage, "ttft_ms": round((first_content_at - request_started) * 1000, 2) if first_content_at else None,
                             "generation_duration_ms": round((completed_at - first_content_at) * 1000, 2) if first_content_at else None,
                             "total_duration_ms": round((completed_at - request_started) * 1000, 2)}
                if telemetry.get("completion_tokens") and telemetry.get("generation_duration_ms"):
                    telemetry["tokens_per_second"] = round(telemetry["completion_tokens"] / (telemetry["generation_duration_ms"] / 1000), 2)
                    telemetry["tokens_per_second_source"] = "calculated"
                if usage:
                    telemetry["usage_source"] = "provider"
                yield {"complete": True, "answer": answer, "sources": sources, "tools_used": tools_used, "tool_rounds": tool_rounds, "telemetry": telemetry}
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
                tool_context = ToolExecutionContext(request.context.conversation_id, request.context.provider_id, request.context.model_id, tool_rounds, request.context.run_id)
                started = time.monotonic()
                event_id = request.event_sink.start_event("ACT", call["name"], {"tool": call["name"], "round": tool_rounds})
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
                duration_ms = round(time.monotonic() - started, 4) * 1000
                event_status = "success" if status == "ok" and not result.get("error") else status
                request.event_sink.finish_event(event_id, event_status, {"tool": call["name"], "round": tool_rounds, **({"error_code": result.get("error", {}).get("code")} if result.get("error") else {})}, duration_ms)
                yield {"trace": {"type": "ACT", "duration_ms": duration_ms, "status": event_status, "metadata": {"tool": call["name"], "round": tool_rounds, "run_id": request.context.run_id}}}
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
