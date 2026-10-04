from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator

from app.agent_model import ModelAdapter, ModelAdapterError
from app.agent_profiles import AgentRunConfiguration
from app.diagnostics import diagnostic
from app.kernel import ToolExecutionContext
from app.tools import EffectiveToolSet, ToolExecutor, ToolNotAvailableError
from app.runtime_trace import NullRuntimeEventSink, RuntimeEventSink
from app.grounding import GROUNDING_INSTRUCTIONS, GroundedContext, KnowledgeOutcome, knowledge_outcome_instruction
from app.tool_results import ToolResultPipeline
from app.interactions import normalize_dialog, validate_schema, validate_values
from app.approvals import tool_fingerprint
from app.progress_guard import HARD_STOP_HINT, RECOVERY_HINT, ProgressGuard, call_fingerprint

logger = logging.getLogger("nexo.agent")


@dataclass(frozen=True)
class AgentRuntimeLimits:
    max_tool_calls: int = 10
    max_tool_output_chars: int = 12000


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


class PolicyEvaluator:
    def evaluate(self, tool: Any) -> PolicyDecision:
        if not tool or tool.action == "unknown" or tool.action is None:
            return PolicyDecision.DENY
        return PolicyDecision.ALLOW if tool.action == "read_only" else PolicyDecision.APPROVAL_REQUIRED


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
    effective_configuration: "EffectiveRunConfiguration | None" = None
    knowledge_outcome: KnowledgeOutcome | None = None
    request_started_at: float | None = None
    interaction_handler: bool = False
    resume_messages: list[dict[str, Any]] | None = None
    initial_tool_call_count: int = 0
    initial_tool_rounds: int = 0
    initial_tools_used: list[str] | None = None
    progress_state: dict[str, Any] | None = None


@dataclass(frozen=True)
class EffectiveRunConfiguration:
    """Immutable Agent + Knowledge snapshot for one execution."""

    agent: AgentRunConfiguration | None
    notebook_id: str | None
    tools: EffectiveToolSet
    grounded_context: GroundedContext | None
    temperature: float | None
    knowledge_outcome: KnowledgeOutcome | None = None
    requested_tool_names: tuple[str, ...] = ()
    available_tool_names: tuple[str, ...] = ()
    max_tool_calls: int = 10


class AgentRuntime:
    def __init__(self, limits: AgentRuntimeLimits | None = None) -> None:
        self.limits = limits or AgentRuntimeLimits()

    async def stream(
        self,
        request: AgentRunRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        configuration = request.effective_configuration
        max_tool_calls = configuration.max_tool_calls if configuration and configuration.agent else self.limits.max_tool_calls
        system_instructions = configuration.agent.system_instructions if configuration and configuration.agent else request.system_instructions
        grounded_context = configuration.grounded_context if configuration else request.grounded_context
        knowledge_outcome = configuration.knowledge_outcome if configuration else request.knowledge_outcome
        effective_tools = configuration.tools if configuration else request.effective_tools
        temperature = configuration.temperature if configuration else request.temperature
        resuming = request.resume_messages is not None
        messages = list(request.resume_messages if resuming else request.messages)
        if not resuming and system_instructions:
            messages.insert(0, {"role": "system", "content": system_instructions})
        if not resuming and sum(message.get("role") == "user" for message in messages) > 1:
            messages.insert(1 if system_instructions else 0, {"role": "system", "content":
                "The latest user message defines the active task. Earlier turns are context, not unfinished instructions, unless the latest message clearly continues them."})
        tool_names = sorted(effective_tools.names)
        if tool_names and not resuming:
            guidance = [f"Available tools for this turn: {', '.join(tool_names)}.",
                        "Use an available tool when it can answer the request; do not claim a capability is unavailable if it is listed.",
                        "After tool calls, interpret their returned data and answer from it. Never invent external facts or show tool-call syntax to the user."]
            if "web_search" in effective_tools.names:
                guidance.append("For current or external facts, use web_search and base conclusions on its results. Do not repeat an equivalent search; after a relevant result, proceed unless a material fact is still missing.")
            if "native.get_current_datetime" in effective_tools.names:
                guidance.append("For relative dates such as today, the next few days, or the last N days, call native.get_current_datetime first.")
            if "native.render_artifact" in effective_tools.names:
                guidance.append("When a chart or table is requested, use native.render_artifact with evidence-backed data after gathering it. If it returns a validation error, correct the exact reported issue and retry at most once; do not switch through alternate artifact types or claim the visualization service is unavailable. If the corrected attempt fails, explain the actual error and provide the data in text.")
            if "native.request_user_input" in effective_tools.names:
                guidance.append("When you need a choice, preference, or clarification before proceeding, call native.request_user_input with a concise structured dialog instead of asking in prose. The user can edit supplied values. Tool calls that are not read-only may also pause for explicit approval.")
            messages.insert(1 if system_instructions else 0, {"role": "system", "content": " ".join(guidance)})
        if grounded_context is not None and not resuming:
            grounding = f"{GROUNDING_INSTRUCTIONS}\n\n{grounded_context.serialize()}"
            messages.insert(1 if system_instructions else 0, {"role": "system", "content": grounding})
        if not resuming and knowledge_outcome is not None and knowledge_outcome != KnowledgeOutcome.GROUNDING_APPLIED:
            messages.insert(1 if system_instructions else 0, {"role": "system", "content": knowledge_outcome_instruction(knowledge_outcome)})
        answer = ""
        sources: list[dict[str, Any]] = []
        tools_used: list[str] = list(request.initial_tools_used or ())
        artifacts: list[dict[str, Any]] = []
        tool_rounds = request.initial_tool_rounds
        tool_call_count = request.initial_tool_call_count
        artifact_render_failures = 0
        blocked_calls: set[tuple[str, str]] = set()
        result_pipeline = ToolResultPipeline(self.limits.max_tool_output_chars)
        request_started = request.request_started_at or time.perf_counter()
        first_content_at: float | None = None
        provider_ttft_ms: float | None = None
        usage: dict[str, int] = {}
        guard = ProgressGuard.restore(request.progress_state)
        force_final = False

        def runtime_event(kind: str, name: str, metadata: dict[str, Any] | None = None, duration_ms: float = 0) -> None:
            event_id = request.event_sink.start_event(kind, name, metadata or {})
            request.event_sink.finish_event(event_id, "completed", metadata or {}, duration_ms)

        for _ in range(max_tool_calls + 2):
            reason_started = time.perf_counter()
            reason_metadata = {"model": request.model.model_id, "round": tool_rounds + 1,
                               "grounding_applied": grounded_context is not None and bool(grounded_context.retrieval_results),
                               "grounding_chunks": len(grounded_context.retrieval_results) if grounded_context else 0,
                               "context_chars": grounded_context.context_chars if grounded_context else 0,
                               **(request.runtime_snapshot or {}),
                               **({"agent_profile_id": request.profile_id} if request.profile_id else {})}
            reason_event_id = request.event_sink.start_event("REASON", request.model.model_id, reason_metadata)
            if tool_rounds:
                runtime_event("PROVIDER_CONTINUATION_STARTED", request.model.model_id, {"round": tool_rounds + 1})
            yield {"activity": {"type": "REASON", "status": "running", "round": tool_rounds + 1}}
            definitions = effective_tools.definitions()
            diagnostic(logger, "agent_runtime", **{
                "tools_available": len(definitions),
                "tools_exposed": len(definitions),
                "exposed_tool_names": [tool["function"]["name"] for tool in definitions],
                "grounding_applied": grounded_context is not None and bool(grounded_context.retrieval_results),
                "grounding_chunks": len(grounded_context.retrieval_results) if grounded_context else 0,
                "grounding_context_chars": grounded_context.context_chars if grounded_context else 0,
                "message_roles": [message.get("role") for message in messages],
            })
            tool_calls: dict[int, dict[str, Any]] = {}
            round_content = ""
            finish_reason = None
            try:
                async for chunk in request.model.stream(messages, definitions if tool_call_count < max_tool_calls and not force_final else [], temperature):
                    finish_reason = chunk.finish_reason or finish_reason
                    if chunk.usage:
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            if isinstance(chunk.usage.get(key), int):
                                usage[key] = usage.get(key, 0) + chunk.usage[key]
                    if chunk.reasoning_content:
                        yield {"thinking_delta": chunk.reasoning_content}
                    if chunk.content:
                        first_content_at = first_content_at or time.perf_counter()
                        provider_ttft_ms = provider_ttft_ms if provider_ttft_ms is not None else chunk.provider_ttft_ms
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
                if not answer.strip():
                    yield {"error": "provider_empty_response"}
                    return
                diagnostic(logger, "agent_loop", tool_rounds=tool_rounds, executed_tool_names=tools_used)
                completed_at = time.perf_counter()
                telemetry = {**usage, "ttft_ms": provider_ttft_ms,
                             "request_to_first_token_ms": round((first_content_at - request_started) * 1000, 2) if first_content_at else None,
                             "generation_duration_ms": round((completed_at - first_content_at) * 1000, 2) if first_content_at else None,
                             "total_duration_ms": round((completed_at - request_started) * 1000, 2)}
                telemetry.update({"provider_ttft_ms": telemetry["ttft_ms"], "generation_ms": telemetry["generation_duration_ms"],
                                  "total_request_ms": telemetry["total_duration_ms"]})
                if telemetry.get("completion_tokens") and telemetry.get("generation_duration_ms"):
                    telemetry["tokens_per_second"] = round(telemetry["completion_tokens"] / (telemetry["generation_duration_ms"] / 1000), 2)
                    telemetry["tokens_per_second_source"] = "calculated"
                if usage:
                    telemetry["usage_source"] = "provider"
                telemetry["progress_guard"] = {"enabled": guard.enabled, **dict(guard.counters), "stagnation_score": guard.score,
                                                "recoveries": guard.recoveries, "hard_stop": guard.hard_stop}
                yield {"complete": True, "answer": answer, "sources": sources, "artifacts": artifacts, "tools_used": tools_used, "tool_rounds": tool_rounds, "telemetry": telemetry,
                       "native_tool_calls": sum(name.startswith("native.") for name in tools_used)}
                return
            runtime_event("TOOL_REQUESTED", "tool calls", {"count": len(tool_calls), "round": tool_rounds + 1})
            if guard.enabled and not force_final:
                blocked = False
                for proposed in tool_calls.values():
                    try:
                        proposed_args = json.loads(proposed["arguments"] or "{}")
                        if (proposed["name"] != "native.render_artifact" and isinstance(proposed_args, dict)
                                and guard.evaluate_pre_call(call_fingerprint(proposed["name"], proposed_args)) == "hard_stop"):
                            blocked = True
                            break
                    except Exception:
                        guard.enabled = False
                        continue
                if blocked:
                    guard.hard_stop = True
                    force_final = True
                    messages.append({"role": "system", "content": HARD_STOP_HINT})
                    stop_event = request.event_sink.start_event("progress_hard_stop", "Progress Guard", {"stagnation_score": guard.score})
                    request.event_sink.finish_event(stop_event, "completed", {"stagnation_score": guard.score}, 0)
                    continue
            if force_final:
                messages.append({"role": "system", "content": HARD_STOP_HINT})
                continue
            if tool_call_count >= max_tool_calls:
                messages.append({"role": "assistant", "content": round_content or None,
                                 "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in tool_calls.values()]})
                for call in tool_calls.values():
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                     "content": "Tool-call budget is exhausted. Do not call more tools; answer using the evidence already collected."})
                force_final = True
                guard.hard_stop = True
                guard.counters["forced_synthesis"] += 1
                runtime_event("forced_synthesis", "Progress Guard", {"tool_calls": guard.counters.get("tool_calls", 0), "evidence_resources_seen": guard.counters.get("evidence_resources_seen", 0)})
                messages.append({"role": "system", "content": HARD_STOP_HINT})
                continue
            messages.append({
                "role": "assistant",
                "content": round_content or None,
                "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in tool_calls.values()],
            })
            tool_rounds += 1
            for call in tool_calls.values():
                if tool_call_count >= max_tool_calls:
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                     "content": "Tool-call budget is exhausted. Do not call more tools; answer using the evidence already collected."})
                    force_final = True
                    guard.hard_stop = True
                    if not guard.counters.get("forced_synthesis"):
                        guard.counters["forced_synthesis"] += 1
                        runtime_event("forced_synthesis", "Progress Guard", {"tool_calls": guard.counters.get("tool_calls", 0), "evidence_resources_seen": guard.counters.get("evidence_resources_seen", 0)})
                    continue
                tool_call_count += 1
                tools_used.append(call["name"])
                tool_context = ToolExecutionContext(request.context.conversation_id, request.context.provider_id, request.context.model_id, tool_rounds, request.context.run_id)
                started = time.monotonic()
                event_id = request.event_sink.start_event("ACT", call["name"], {"tool": call["name"], "round": tool_rounds})
                yield {"activity": {"type": "ACT", "status": "running", "tool": call["name"], "round": tool_rounds}}
                status = "ok"
                result = None
                definition = effective_tools.definition(call["name"])
                decision = PolicyEvaluator().evaluate(definition) if definition else PolicyDecision.DENY
                runtime_event("TOOL_POLICY", call["name"], {"decision": decision.value, "round": tool_rounds})
                call_identity = None
                guard_started = time.perf_counter()
                if guard.enabled and call["name"] not in {"native.render_artifact", "native.request_user_input"}:
                    try:
                        parsed_args = json.loads(call["arguments"] or "{}")
                        if isinstance(parsed_args, dict):
                            call_identity = call_fingerprint(call["name"], parsed_args)
                            if decision != PolicyDecision.ALLOW:
                                guard.pending = guard.pending_call(call_identity, call["name"], parsed_args)
                            else:
                                guard.observe_call(call_identity, call["name"], parsed_args)
                    except Exception:
                        guard.enabled = False
                if guard.enabled:
                    guard.counters["guard_overhead_ms"] += round((time.perf_counter() - guard_started) * 1000, 4)
                if call["name"] == "native.render_artifact":
                    if artifact_render_failures >= 2:
                        result = {"error": {"code": "artifact_retry_limit", "message": "El renderizador ha fallado dos veces seguidas. No vuelvas a invocarlo; explica el problema concreto y presenta los datos en texto."}}
                        status = "artifact_retry_limit"
                arguments: dict[str, Any] = {}
                if result is None:
                    try:
                        arguments = json.loads(call["arguments"] or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError
                    except (ValueError, TypeError, json.JSONDecodeError):
                        result = {"error": {"code": "invalid_arguments", "message": "Argumentos de herramienta inválidos."}}
                        status = "invalid_arguments"
                    else:
                        execution_started = None
                        execution_event_id = None
                        try:
                            execution_started = time.perf_counter()
                            execution_event_id = request.event_sink.start_event("TOOL_EXECUTION_STARTED", call["name"], {"round": tool_rounds})
                            if definition is None:
                                result = {"error": {"code": "provider_requested_unexposed_tool", "message": "La herramienta no está disponible para esta ejecución."}}
                                status = "tool_not_exposed_for_run"
                            elif call["name"] == "native.request_user_input":
                                dialog = normalize_dialog(arguments.get("dialog"))
                                if not request.interaction_handler:
                                    result = {"error": {"code": "interaction_unavailable", "message": "User interaction is unavailable in this run."}}
                                else:
                                    response = yield {"interaction": {"kind": "user_input", "payload": dialog, "validation_schema": {"fields": dialog["fields"]}}}
                                    if not response or not response.get("approved"):
                                        if response and response.get("error"):
                                            result = {"error": {"code": "interaction_expired", "message": "The user interaction expired before a response was received."}}
                                        else:
                                            result = {"cancelled": True, "content": "The user cancelled this question."}
                                    else:
                                        values = validate_values(dialog["fields"], response.get("values"))
                                        result = {"content": "User response received.", "structured_data": values}
                            elif decision == PolicyDecision.DENY:
                                result = {"error": {"code": "unclassified_tool", "message": "This tool is not classified as read-only or state-changing, so it was not executed."}}
                                status = "unclassified_tool"
                            elif decision == PolicyDecision.APPROVAL_REQUIRED:
                                signature = (call["name"], call["arguments"])
                                if signature in blocked_calls:
                                    result = {"error": {"code": "repeated_approval_required", "message": "The same tool call was denied or blocked again; execution stopped."}}
                                    status = "repeated_approval_required"
                                elif not request.interaction_handler:
                                    blocked_calls.add(signature)
                                    result = {"error": {"code": "approval_unavailable", "message": "This action requires explicit user approval, but interaction is unavailable in this run."}}
                                    status = "approval_required"
                                else:
                                    duration_ms = round(time.monotonic() - started, 4) * 1000
                                    request.event_sink.finish_event(event_id, "waiting_approval", {"tool": call["name"], "round": tool_rounds, "action": definition.action or "unknown", "approval_required": True}, duration_ms)
                                    yield {"trace": {"type": "ACT", "name": call["name"], "status": "waiting_approval", "duration_ms": duration_ms,
                                        "metadata": {"tool": call["name"], "action": definition.action or "unknown", "policy_decision": decision.value, "run_id": request.context.run_id}}}
                                    yield {"approval_required": {
                                         "tool": call["name"], "tool_call_id": call["id"],
                                          "action": definition.action or "unknown", "description": definition.description, "arguments": arguments,
                                         "tool_fingerprint": tool_fingerprint(definition),
                                        "continuation": {"messages": messages, "tool_call_count": tool_call_count,
                                            "tool_rounds": tool_rounds, "tools_used": tools_used,
                                            "max_tool_calls": max_tool_calls,
                                            "tool_names": sorted(effective_tools.names),
                                            "provider_id": request.context.provider_id,
                                            "provider_base_url": (request.runtime_snapshot or {}).get("provider_base_url"),
                                            "model_id": request.context.model_id,
                                             "temperature": temperature,
                                             "progress_state": guard.snapshot(),
                                            "profile_id": request.profile_id,
                                            "profile_name": request.profile_name,
                                            "runtime_snapshot": request.runtime_snapshot or {}},
                                    }}
                                    return
                            else:
                                    result = await request.tool_executor.invoke(effective_tools, call["name"], tool_context, arguments)
                        except (ValueError, TypeError, json.JSONDecodeError) as exc:
                            result = {"error": {"code": "invalid_arguments", "message": str(exc)[:300]}}
                            status = "invalid_arguments"
                        except ToolNotAvailableError:
                            result = {"error": {"code": "tool_not_available", "message": "La herramienta no está disponible para esta ejecución."}}
                            status = "tool_not_available"
                        except Exception:
                            result = {"error": {"code": "tool_execution_error", "message": "La herramienta no pudo completar la operación."}}
                            status = "tool_execution_error"
                            logger.exception("tool execution failed", extra={"conversation_id": request.context.conversation_id, "provider_id": request.context.provider_id, "model_id": request.model.model_id, "tool": call["name"], "round": tool_rounds})
                        finally:
                            if execution_started is not None:
                                execution_duration = (time.perf_counter() - execution_started) * 1000
                                request.event_sink.finish_event(execution_event_id, "completed", {"round": tool_rounds}, execution_duration)
                                runtime_event("TOOL_EXECUTION_COMPLETED", call["name"], {"round": tool_rounds}, execution_duration)
                if not isinstance(result, dict):
                    result = {"error": {"code": "tool_execution_error", "message": "La herramienta devolvió un resultado inválido."}}
                    status = "tool_execution_error"
                if result.get("error") and status == "ok":
                    status = result["error"].get("code", "tool_error")
                if call["name"] == "native.render_artifact":
                    artifact_render_failures = artifact_render_failures + 1 if result.get("error") else 0
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
                if isinstance(result.get("artifacts"), list):
                    for artifact in result["artifacts"]:
                        if len(artifacts) >= 5:
                            break
                        artifacts.append(artifact)
                        yield {"artifact": artifact}
                    result = {key: value for key, value in result.items() if key != "artifacts"}
                current_query = next((message.get("content", "") for message in reversed(messages) if message.get("role") == "user"), "")
                if isinstance(current_query, list):
                    current_query = " ".join(part.get("text", "") for part in current_query if isinstance(part, dict))
                canonical, pipeline_metadata = result_pipeline.process(
                    result, query=str(current_query), arguments=arguments if "arguments" in locals() and isinstance(arguments, dict) else {},
                    projector=definition.result_projector if definition else None)
                projection = pipeline_metadata.pop("projection", canonical)
                runtime_event("tool_result_projected", call["name"], {"original_size": pipeline_metadata["original_size"],
                    "projected_size": pipeline_metadata["projected_size"], "truncated": pipeline_metadata["truncated"],
                    "projection_strategy": pipeline_metadata["projection_strategy"],
                    "original_record_count": pipeline_metadata["original_record_count"],
                    "projected_record_count": pipeline_metadata["projected_record_count"],
                    "fields_preserved_count": pipeline_metadata["fields_preserved_count"]})
                progress_action = "allow"
                if guard.enabled and call_identity:
                    prior_score = guard.score
                    guard_started = time.perf_counter()
                    try:
                            progress = guard.observe_result(projection, status, duration_ms, truncated=pipeline_metadata["truncated"])
                    except Exception:
                        guard.enabled = False
                        logger.exception("progress guard failed open", extra={"conversation_id": request.context.conversation_id, "run_id": request.context.run_id})
                        progress = {"action": "allow", "delta": 0}
                    guard.counters["guard_overhead_ms"] += round((time.perf_counter() - guard_started) * 1000, 4)
                    progress_action = progress["action"]
                    runtime_event("research_progress_evaluated", "Progress Guard", {"action": progress_action, "stagnation_score": guard.score,
                        "evidence_resources_seen": guard.counters.get("evidence_resources_seen", 0), "low_progress_events": guard.counters.get("low_progress_events", 0),
                        "repeated_truncation": guard.counters.get("repeated_truncation", 0)})
                    trace_metadata = {"tool_calls": guard.counters.get("tool_calls", 0), "unique_calls": guard.counters.get("unique_calls", 0),
                                      "unique_results": guard.counters.get("unique_results", 0), "exact_repeats": guard.counters.get("exact_repeats", 0),
                                      "repeated_results": guard.counters.get("repeated_results", 0), "cycles_detected": guard.counters.get("cycles_detected", 0),
                                      "repeated_failures": guard.counters.get("repeated_failures", 0), "stagnation_score": guard.score}
                    trace_metadata["guard_overhead_ms"] = guard.counters.get("guard_overhead_ms", 0)
                    if progress["action"] == "recover":
                        recovery_event = request.event_sink.start_event("recovery_injected", "Progress Guard", trace_metadata)
                        request.event_sink.finish_event(recovery_event, "completed", trace_metadata, 0)
                    elif progress["action"] == "hard_stop":
                        force_final = True
                        guard.counters["forced_synthesis"] += 1
                        runtime_event("forced_synthesis", "Progress Guard", {"stagnation_score": guard.score})
                        stop_event = request.event_sink.start_event("progress_hard_stop", "Progress Guard", trace_metadata)
                        request.event_sink.finish_event(stop_event, "completed", trace_metadata, 0)
                    elif prior_score > guard.score:
                        recovered_event = request.event_sink.start_event("progress_recovered", "Progress Guard", trace_metadata)
                        request.event_sink.finish_event(recovered_event, "completed", trace_metadata, 0)
                result_text = json.dumps(projection, ensure_ascii=False, separators=(",", ":"))
                diagnostic(logger, "tool_result_pipeline", tool=call["name"], original_size=pipeline_metadata["original_size"], projected_size=pipeline_metadata["projected_size"], compacted=pipeline_metadata["compacted"], truncated=pipeline_metadata["truncated"], projection_strategy=pipeline_metadata["projection_strategy"], original_record_count=pipeline_metadata["original_record_count"], projected_record_count=pipeline_metadata["projected_record_count"], fields_preserved_count=pipeline_metadata["fields_preserved_count"], structured_result=isinstance(canonical.get("structured_data", canonical.get("data")), (dict, list)))
                diagnostic(logger, "tool_policy", tool=call["name"], source=definition.source if definition else "unknown", action=definition.action if definition else "unknown", policy_decision=decision.value, execution_attempted=status not in {"approval_required", "repeated_approval_required"}, execution_status=status, duration_ms=duration_ms)
                request.event_sink.finish_event(event_id, event_status, {"tool": call["name"], "round": tool_rounds, "source": definition.source if definition else "unknown", "action": definition.action if definition else "unknown", "policy_decision": decision.value, "execution_attempted": status not in {"approval_required", "repeated_approval_required"}, "execution_status": status, "original_size": pipeline_metadata["original_size"], "projected_size": pipeline_metadata["projected_size"], "compacted": pipeline_metadata["compacted"], "truncated": pipeline_metadata["truncated"], "structured_result": isinstance(canonical.get("structured_data"), (dict, list))}, duration_ms)
                if result.get("error"):
                    yield {"status": "tool_error", "tool": call["name"], "message": result["error"].get("message", "Error de herramienta")}
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": result_text})
                if progress_action == "recover":
                    messages.append({"role": "system", "content": RECOVERY_HINT})
                elif progress_action == "hard_stop":
                    messages.append({"role": "system", "content": HARD_STOP_HINT})
                logger.info("tool call", extra={"conversation_id": request.context.conversation_id, "provider_id": request.context.provider_id, "model_id": request.context.model_id, "tool": call["name"], "round": tool_rounds, "status": status, "duration": round(time.monotonic() - started, 4)})
                if status == "repeated_approval_required":
                    yield {"error": "The same tool call was blocked by its action policy and was already stopped."}
                    return
            diagnostic(logger, "agent_loop", tool_rounds=tool_rounds, executed_tool_names=tools_used)
        yield {"error": "Se alcanzó el límite configurado de llamadas a herramientas.", "error_code": "tool_call_limit"}
