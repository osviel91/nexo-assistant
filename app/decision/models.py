from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


QuestionType = Literal["boolean", "choice", "score"]


class DecisionQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    type: QuestionType
    statement: str = Field(min_length=1, max_length=4000)
    options: dict[str, Any] | list[Any] | None = None
    scale: list[Any] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "DecisionQuestion":
        if self.type == "choice":
            if not self.options or not 2 <= len(self.options) <= 255:
                raise ValueError("choice questions require 2 to 255 options")
        elif self.type == "score":
            if not self.scale or not 2 <= len(self.scale) <= 10:
                raise ValueError("score questions require 2 to 10 scale levels")
        elif self.options is not None or self.scale is not None:
            raise ValueError("boolean questions do not accept options or scale")
        return self


class DecisionContext(BaseModel):
    conversation_id: str | None = None


class DecisionRequest(BaseModel):
    state: Any
    questions: list[DecisionQuestion] = Field(min_length=1, max_length=255)
    context: DecisionContext | None = None

    @model_validator(mode="after")
    def unique_question_ids(self) -> "DecisionRequest":
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        return self


class DecisionAnswer(BaseModel):
    type: QuestionType
    value: Any
    probabilities: dict[str, float] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class DecisionResult(BaseModel):
    model: str | None = None
    answers: dict[str, DecisionAnswer]
    metadata: dict[str, Any] | None = None
