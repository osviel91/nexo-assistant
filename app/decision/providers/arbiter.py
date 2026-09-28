from __future__ import annotations

import httpx
from typing import Callable

from app.decision.models import DecisionAnswer, DecisionRequest, DecisionResult


class ArbiterError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ArbiterDecisionProvider:
    name = "arbiter"

    def __init__(self, url: str, api_key: str, timeout: float, model: str = "jev-latest", client_factory=httpx.AsyncClient, raw_response_sink: Callable[[object], None] | None = None) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.model = model
        self.client_factory = client_factory
        self.raw_response_sink = raw_response_sink

    @property
    def endpoint(self) -> str:
        return self.url + ("/systemone" if self.url.endswith("/v1") else "/v1/systemone")

    @property
    def readiness_endpoint(self) -> str:
        base = self.url[:-3] if self.url.endswith("/v1") else self.url
        return base + "/readyz"

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        payload = {
            "state": request.state,
            "model": self.model,
            "questions": {question.id: self._question_payload(question) for question in request.questions},
        }
        try:
            async with self.client_factory(timeout=self.timeout) as client:
                response = await client.post(self.endpoint, headers=self._headers(), json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ArbiterError("decision_timeout") from exc
        except httpx.HTTPStatusError as exc:
            raise ArbiterError("decision_provider_unavailable") from exc
        except httpx.RequestError as exc:
            raise ArbiterError("decision_provider_unavailable") from exc
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise ArbiterError("decision_invalid_response") from exc
        if self.raw_response_sink is not None:
            self.raw_response_sink(payload)
        return self._parse_response(payload, request)

    async def check_available(self) -> bool:
        if not self.url:
            return False
        try:
            async with self.client_factory(timeout=self.timeout) as client:
                response = await client.get(self.readiness_endpoint, headers=self._headers())
            return response.is_success
        except (httpx.HTTPError, OSError):
            return False

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @staticmethod
    def _question_payload(question) -> dict:
        if question.type == "boolean":
            # System One intentionally calls the boolean primitive "noul".
            return {"type": "noul", "instructions": question.statement}
        if question.type == "choice":
            options = question.options
            criteria = options if isinstance(options, dict) else {str(option): None for option in options}
            return {"type": "choice", "instructions": question.statement, "criteria": criteria}
        return {"type": "score", "instructions": question.statement, "criteria": question.scale}

    @staticmethod
    def _parse_response(payload: object, request: DecisionRequest) -> DecisionResult:
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise ArbiterError("decision_invalid_response")
        answers = {}
        for question in request.questions:
            raw = payload["answers"].get(question.id)
            try:
                answers[question.id] = ArbiterDecisionProvider._parse_answer(question, raw)
            except (KeyError, TypeError, ValueError):
                raise ArbiterError("decision_invalid_response") from None
        model = payload.get("model")
        metadata = {}
        if isinstance(payload.get("routing"), str):
            metadata["routing"] = payload["routing"]
        if isinstance(payload.get("latency_ms"), (int, float)) and not isinstance(payload["latency_ms"], bool):
            metadata["latency_ms"] = payload["latency_ms"]
        return DecisionResult(
            model=model if isinstance(model, str) else None,
            answers=answers,
            metadata=metadata or None,
        )

    @staticmethod
    def _parse_answer(question, raw: object) -> DecisionAnswer:
        if not isinstance(raw, dict):
            raise ValueError
        if question.type == "boolean":
            probability = raw.get("noul")
            if not isinstance(probability, (int, float)) or not 0 <= probability <= 1:
                raise ValueError
            p = float(probability)
            return DecisionAnswer(type="boolean", value=p >= 0.5, probabilities={"true": p, "false": round(1 - p, 12)}, confidence=max(p, 1 - p))
        if raw.get("type") != question.type:
            raise ValueError
        probabilities = raw.get("probabilities")
        confidence = raw.get("confidence")
        if not isinstance(probabilities, dict) or not all(isinstance(v, (int, float)) for v in probabilities.values()):
            raise ValueError
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError
        if question.type == "choice":
            value = raw.get("choice")
            if value not in question.options:
                raise ValueError
        else:
            value = raw.get("score")
            if not isinstance(value, (int, float)) and value not in question.scale:
                raise ValueError
        return DecisionAnswer(type=question.type, value=value, probabilities={str(k): float(v) for k, v in probabilities.items()}, confidence=float(confidence))
