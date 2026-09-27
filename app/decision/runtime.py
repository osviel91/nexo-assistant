from __future__ import annotations

import asyncio
import logging
import time

from app.decision.models import DecisionRequest, DecisionResult
from app.decision.providers.arbiter import ArbiterError
from app.decision.providers.base import DecisionProvider

logger = logging.getLogger("nexo.decision")


class DecisionRuntimeError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DecisionRuntime:
    def __init__(self, provider: DecisionProvider, timeout: float) -> None:
        self.provider = provider
        self.timeout = timeout

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(self.provider.decide(request), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise DecisionRuntimeError("decision_timeout") from exc
        except ArbiterError as exc:
            raise DecisionRuntimeError(exc.code) from exc
        except Exception as exc:
            logger.exception("decision provider failed", extra={"provider": self.provider.name})
            raise DecisionRuntimeError("decision_provider_unavailable") from exc
        if not isinstance(result, DecisionResult):
            raise DecisionRuntimeError("decision_invalid_response")
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info("decision completed", extra={"provider": self.provider.name, "questions": len(request.questions), "status": "success", "duration_ms": duration_ms})
        return result
