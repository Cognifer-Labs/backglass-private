"""The personalization interview. Phase 6 S7.

Two model calls, hard-bounded, degrading: questions first, then — after the owner
answers in the terminal — the adjustment set. Any failure at any point returns
None and the caller instantiates the raw preset; a half-personalized roadmap is
never written because `instantiate` applies preset and adjustments in one
transaction.

This is the one model call outside a sync (deviation recorded in tasks/todo.md
§Phase 6): it goes through the same ModelClient, its prompts are versioned spec
files, its transcript is persisted as an immutable source_item, and its spend
lands in a run(kind='interview') row so the monthly cap counts it.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import date
from hashlib import sha256

from backglass.config import Settings
from backglass.db import now_iso
from backglass.extract.client import ModelClient, ModelError
from backglass.extract.prompts import load as load_prompt
from backglass.extract.schemas import json_schema
from backglass.ledger import USER_ID
from backglass.roadmap import schemas
from backglass.roadmap.instantiate import (
    AddedStep,
    Adjustments,
    CadenceCount,
    StepAction,
)
from backglass.roadmap.presets import Preset
from backglass.sync import SpendCap

QUESTIONS_PROMPT = "roadmap-interview-questions"
ADJUST_PROMPT = "roadmap-interview-adjust"


@dataclasses.dataclass
class InterviewResult:
    adjustments: Adjustments | None  # None == degraded, use the raw preset
    transcript: str
    spend_usd: float
    stamp: str
    degraded_reason: str | None = None


def _preset_json(preset: Preset) -> str:
    return json.dumps(
        {
            "id": preset.id,
            "title": preset.title,
            "definition_of_done": preset.definition_of_done,
            "steps": [dataclasses.asdict(s) for s in preset.steps],
            "cadences": [dataclasses.asdict(c) for c in preset.cadences],
        },
        indent=2,
    )


def _to_adjustments(validated: schemas.RoadmapAdjustments) -> Adjustments:
    """Schema output → engine dataclasses. Dates that fail to parse drop their item —
    a model output is evidence to be checked, not instructions to be obeyed."""

    def _date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    steps = []
    for s in validated.step_adjustments:
        parsed = _date(s.planned_date)
        if s.action == "redate" and parsed is None:
            continue
        steps.append(StepAction(step_key=s.step_key, action=s.action, planned_date=parsed))
    added = []
    for a in validated.added_steps:
        parsed = _date(a.planned_date)
        if parsed is None:
            continue
        added.append(
            AddedStep(
                key=a.key, title=a.title, planned_date=parsed,
                after_step_key=a.after_step_key,
            )
        )
    cadences = [
        CadenceCount(cadence_key=c.cadence_key, weekly_count=c.weekly_count)
        for c in validated.cadence_adjustments
    ]
    return Adjustments(
        step_actions=steps, added_steps=added, cadence_counts=cadences,
        summary=validated.summary,
    )


def run_interview(
    conn: sqlite3.Connection,
    settings: Settings,
    client: ModelClient,
    preset: Preset,
    today: date,
    *,
    ask: Callable[[str], str],
    say: Callable[[str], None] = lambda _: None,
) -> InterviewResult:
    """Bounded loop: cap check → questions → answers → adjustments.

    `ask` is injected (typer.prompt in the CLI, a canned function in tests) so no
    test needs a terminal and none touches a live model.
    """
    questions_prompt = load_prompt(QUESTIONS_PROMPT)
    stamp = load_prompt(ADJUST_PROMPT).stamp
    cap = SpendCap(conn, settings, client)
    spent = 0.0

    if cap.reached:
        return InterviewResult(
            adjustments=None, transcript="", spend_usd=0.0, stamp=stamp,
            degraded_reason="spend cap reached — starting the un-personalized preset",
        )
    # The interview is exactly two calls; a configured ceiling below that means the
    # owner has asked for no interview at all.
    if settings.interview_max_calls < 2:
        return InterviewResult(
            adjustments=None, transcript="", spend_usd=0.0, stamp=stamp,
            degraded_reason="interview_max_calls < 2 — using the preset as written",
        )

    preset_json = _preset_json(preset)
    try:
        first = client.complete(
            system=questions_prompt.render(
                preset_json=preset_json, today=today.isoformat(),
                owner_name=settings.owner_name or "the owner",
            ),
            user=f"Generate the interview questions for the '{preset.id}' path.",
            schema=json_schema(schemas.InterviewQuestions),
            model=settings.model_interview,
            budget_usd=settings.interview_call_budget_usd,
        )
    except ModelError as exc:
        return InterviewResult(
            adjustments=None, transcript="", spend_usd=0.0, stamp=stamp,
            degraded_reason=f"question generation failed ({exc}) — using the preset as written",
        )
    spent += first.cost_usd
    questions = schemas.InterviewQuestions.model_validate(first.data).questions

    lines: list[str] = []
    for q in questions:
        answer = ask(q.question).strip()
        lines.append(f"Q ({q.key}): {q.question}")
        lines.append(f"A: {answer or '(declined)'}")
    transcript = "\n".join(lines)

    cap.charge(spent)
    if cap.reached:
        return InterviewResult(
            adjustments=None, transcript=transcript, spend_usd=spent, stamp=stamp,
            degraded_reason="spend cap reached mid-interview — using the preset as written",
        )

    try:
        second = client.complete(
            system=load_prompt(ADJUST_PROMPT).render(
                preset_json=preset_json, today=today.isoformat(), qa_transcript=transcript
            ),
            user="Return the adjustment set.",
            schema=json_schema(schemas.RoadmapAdjustments),
            model=settings.model_interview,
            budget_usd=settings.interview_call_budget_usd,
        )
    except ModelError as exc:
        return InterviewResult(
            adjustments=None, transcript=transcript, spend_usd=spent, stamp=stamp,
            degraded_reason=f"adjustment call failed ({exc}) — using the preset as written",
        )
    spent += second.cost_usd
    validated = schemas.RoadmapAdjustments.model_validate(second.data)
    say(validated.summary)
    return InterviewResult(
        adjustments=_to_adjustments(validated), transcript=transcript,
        spend_usd=spent, stamp=stamp,
    )


def persist(
    conn: sqlite3.Connection, roadmap_id: int, result: InterviewResult
) -> int | None:
    """Transcript → immutable source_item; spend → run(kind='interview').

    Called after `instantiate` committed. A degraded interview with no transcript
    persists only the spend (which may itself be zero — then nothing is written,
    keeping re-runs write-free).
    """
    source_item_id: int | None = None
    if result.transcript:
        cur = conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, content_hash, triage_verdict,"
            " extraction_version)"
            " VALUES (?, 'roadmap-interview', ?, ?, ?, 'owner', ?, ?, ?, 'keep', ?)",
            (USER_ID, uuid.uuid4().hex, now_iso(), now_iso(),
             f"Roadmap interview · roadmap {roadmap_id}", result.transcript,
             sha256(result.transcript.encode()).hexdigest(), result.stamp),
        )
        source_item_id = int(cur.lastrowid or 0)
        conn.execute(
            "UPDATE roadmap SET interview_source_item_id = ?, personalized = ? WHERE id = ?",
            (source_item_id, int(result.adjustments is not None), roadmap_id),
        )
    if result.spend_usd > 0:
        conn.execute(
            "INSERT INTO run (user_id, started_at, finished_at, spend_cents, kind)"
            " VALUES (?, ?, ?, ?, 'interview')",
            (USER_ID, now_iso(), now_iso(), round(result.spend_usd * 100)),
        )
    return source_item_id
