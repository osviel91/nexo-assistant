from __future__ import annotations

from typing import Protocol

from app.decision.models import DecisionRequest, DecisionResult


class DecisionProvider(Protocol):
    name: str

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        ...

    async def check_available(self) -> bool:
        ...
