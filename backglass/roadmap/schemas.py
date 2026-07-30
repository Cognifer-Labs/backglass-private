"""Interview output shapes. Pydantic is the single definition; the JSON Schema the
model is forced through is generated from these, same as extraction."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from backglass.extract.schemas import Strict


class InterviewQuestion(Strict):
    key: str
    question: str


class InterviewQuestions(Strict):
    questions: list[InterviewQuestion] = Field(min_length=1, max_length=6)


class StepAdjustment(Strict):
    step_key: str
    action: Literal["keep", "skip", "redate"]
    planned_date: str | None = None  # ISO date; required when action == "redate"
    reason: str


class AddedStepOut(Strict):
    key: str
    title: str
    planned_date: str
    after_step_key: str | None = None
    reason: str


class CadenceAdjustment(Strict):
    cadence_key: str
    weekly_count: int = Field(ge=0, le=21)
    reason: str


class RoadmapAdjustments(Strict):
    step_adjustments: list[StepAdjustment] = Field(default_factory=list)
    added_steps: list[AddedStepOut] = Field(default_factory=list, max_length=5)
    cadence_adjustments: list[CadenceAdjustment] = Field(default_factory=list)
    summary: str
