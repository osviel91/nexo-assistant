from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable

from fastapi import HTTPException
from pydantic import ValidationError

from app.decision.models import DecisionRequest, ShadowDecision
from app.decision.providers.arbiter import ArbiterDecisionProvider
from app.decision.runtime import DecisionRuntime, DecisionRuntimeError
from app.kernel import InterfaceExtension, ModuleContext, ModuleManifest

logger = logging.getLogger("nexo.decision.module")


class DecisionRuntimeModule:
    manifest = ModuleManifest(
        id="decision-runtime",
        name="Optional typed decision runtime",
        version="1.0.0",
        api_version=1,
        capabilities=("decision",),
    )
    interface_extensions = (InterfaceExtension("decisions", "endpoint", "Typed decisions"),)

    def __init__(self, provider: Any | None = None, runtime: DecisionRuntime | None = None, provider_factory: Callable[..., Any] | None = None) -> None:
        self.provider = provider
        self.runtime = runtime
        self.provider_factory = provider_factory or ArbiterDecisionProvider
        self.available = False
        self.last_error: str | None = None
        self._timeout = 10.0
        self.shadow_enabled = False

    def register(self, context: ModuleContext) -> None:
        self._timeout = _float_setting(context.settings, "decision_timeout", os.getenv("NEXO_DECISION_TIMEOUT", "10"), 0.1)
        self.shadow_enabled = _bool_setting(context.settings, "decision_shadow", os.getenv("NEXO_DECISION_SHADOW", "false"))
        if self.runtime is None:
            provider_name = str(context.settings.get("decision_provider", os.getenv("NEXO_DECISION_PROVIDER", "arbiter"))).lower()
            if provider_name != "arbiter":
                raise ValueError(f"unsupported decision provider: {provider_name}")
            self.provider = self.provider or self.provider_factory(
                str(context.settings.get("arbiter_url", os.getenv("NEXO_ARBITER_URL", ""))),
                str(context.settings.get("arbiter_api_key", os.getenv("NEXO_ARBITER_API_KEY", ""))),
                self._timeout,
                str(context.settings.get("decision_model", os.getenv("NEXO_DECISION_MODEL", "jev-latest"))),
            )
            self.runtime = DecisionRuntime(self.provider, self._timeout)
        context.services["decision"] = self.runtime
        context.services["decision-runtime"] = self.runtime
        context.services["decision-shadow"] = self
        context.app.post("/api/decisions")(self.decide_endpoint)

    def startup(self, context: ModuleContext) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.startup_async())
        else:
            # FastAPI calls the synchronous kernel lifecycle inside its event loop.
            asyncio.create_task(self.startup_async())

    async def startup_async(self) -> None:
        try:
            self.available = bool(await self.provider.check_available())
            self.last_error = None if self.available else "decision_provider_unavailable"
        except Exception:
            self.available = False
            self.last_error = "decision_provider_unavailable"

    def shutdown(self, context: ModuleContext) -> None:
        return None

    def run_hook(self, hook: str, context: ModuleContext, payload: object) -> None:
        return None

    def catalog_status(self) -> dict[str, Any]:
        return {"provider": self.provider.name, "available": self.available, "last_error": self.last_error, "shadow_enabled": self.shadow_enabled}

    async def shadow_decide(self, message: str, available_tools: list[str]) -> ShadowDecision:
        started = time.perf_counter()
        request = DecisionRequest(
            state={"message": message, "available_tools": available_tools},
            questions=[
                {"id": "needs_web", "type": "boolean", "statement": "Would this request materially benefit from current or external information obtained through web search?"},
                {"id": "needs_tools", "type": "boolean", "statement": "Would solving this request materially benefit from using one of the available agent tools?"},
                {"id": "task_type", "type": "choice", "statement": "What is the primary type of this request?", "options": ["conversation", "knowledge", "research", "coding", "analysis", "other"]},
            ],
        )
        result = await self.runtime.decide(request)
        metadata = result.metadata or {}
        return ShadowDecision("shadow", result.model, {key: value.model_dump(exclude_none=True) for key, value in result.answers.items()}, {key: metadata[key] for key in ("routing", "latency_ms") if key in metadata}, round((time.perf_counter() - started) * 1000, 2))

    async def decide_endpoint(self, payload: dict[str, Any]) -> Any:
        try:
            request = DecisionRequest.model_validate(payload)
        except ValidationError:
            raise HTTPException(422, {"code": "decision_invalid_request", "message": "Invalid decision request."}) from None
        try:
            result = await self.runtime.decide(request)
            return result.model_dump(exclude_none=True)
        except DecisionRuntimeError as exc:
            status = 504 if exc.code == "decision_timeout" else 502
            raise HTTPException(status, {"code": exc.code, "message": "Decision provider unavailable."}) from None


def _float_setting(settings: dict[str, Any], key: str, raw: str, minimum: float) -> float:
    try:
        return max(float(settings.get(key, raw)), minimum)
    except (TypeError, ValueError):
        return minimum


def _bool_setting(settings: dict[str, Any], key: str, raw: str) -> bool:
    value = settings.get(key, raw)
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}
