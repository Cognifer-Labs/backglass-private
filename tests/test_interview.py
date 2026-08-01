"""The roadmap interview: fake client only, degradation everywhere. Phase 6 S7."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from backglass.config import Settings
from backglass.extract.client import ModelError, ModelResult
from backglass.roadmap import instantiate, interview, presets

TODAY = date(2026, 7, 30)

QUESTIONS = {
    "questions": [
        {"key": "stage", "question": "Where are you on this path today?"},
        {"key": "timeline", "question": "When do you want to sit the MCAT?"},
    ]
}
ADJUSTMENTS = {
    "step_adjustments": [
        {"step_key": "prereqs", "action": "skip", "reason": "already complete"},
        {"step_key": "mcat", "action": "redate", "planned_date": "2026-11-15",
         "reason": "January sitting is full"},
        {"step_key": "no-such-step", "action": "skip", "reason": "hallucinated"},
    ],
    "added_steps": [],
    "cadence_adjustments": [
        {"cadence_key": "practice_sections", "weekly_count": 5, "reason": "full-time prep"},
    ],
    "summary": "Skipped prerequisites, moved the MCAT to November, raised practice to 5/wk.",
}


class ScriptedClient:
    """Returns canned responses in order; raises where the script says to."""

    def __init__(self, script: list[Any], cost_usd: float = 0.02):
        self.script = list(script)
        self.cost_usd = cost_usd
        self.calls = 0

    def complete(self, **kwargs: Any) -> ModelResult:
        del kwargs
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return ModelResult(data=step, cost_usd=self.cost_usd)


def _answers(_q: str) -> str:
    return "done with prereqs, MCAT in November"


class TestDegradation:
    def test_cap_already_reached_makes_no_calls(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from datetime import UTC, datetime

        # SpendCap windows on the current calendar month, so the seeded spend must be
        # "now", not a fixed date — a July 30th literal broke the suite on August 1st.
        conn.execute(
            "INSERT INTO run (started_at, spend_cents) VALUES (?, ?)",
            (datetime.now(UTC).isoformat(), settings.monthly_spend_cap_cents),
        )
        client = ScriptedClient([QUESTIONS, ADJUSTMENTS])
        result = interview.run_interview(
            conn, settings, client, presets.load("medical"), TODAY, ask=_answers
        )
        assert result.adjustments is None
        assert "spend cap" in (result.degraded_reason or "")
        assert client.calls == 0

    def test_model_error_on_first_call_degrades(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        client = ScriptedClient([ModelError("cli exploded")])
        result = interview.run_interview(
            conn, settings, client, presets.load("medical"), TODAY, ask=_answers
        )
        assert result.adjustments is None and client.calls == 1

    def test_model_error_on_second_call_keeps_transcript(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        client = ScriptedClient([QUESTIONS, ModelError("budget exceeded")])
        result = interview.run_interview(
            conn, settings, client, presets.load("medical"), TODAY, ask=_answers
        )
        assert result.adjustments is None
        assert "MCAT" in result.transcript
        assert result.spend_usd > 0

    def test_degraded_interview_never_half_applies(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        client = ScriptedClient([ModelError("down")])
        preset = presets.load("medical")
        result = interview.run_interview(
            conn, settings, client, preset, TODAY, ask=_answers
        )
        rid = instantiate.instantiate(conn, settings, preset, TODAY, result.adjustments)
        skipped = conn.execute(
            "SELECT COUNT(*) AS n FROM roadmap_step WHERE roadmap_id = ? "
            "AND status = 'skipped'", (rid,),
        ).fetchone()["n"]
        assert skipped == 0  # raw preset, nothing personalized


class TestHappyPath:
    def test_adjustments_applied_and_unknown_keys_dropped(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        client = ScriptedClient([QUESTIONS, ADJUSTMENTS])
        preset = presets.load("medical")
        result = interview.run_interview(
            conn, settings, client, preset, TODAY, ask=_answers
        )
        assert result.adjustments is not None
        rid = instantiate.instantiate(conn, settings, preset, TODAY, result.adjustments)

        rows = {
            r["step_key"]: r
            for r in conn.execute(
                "SELECT step_key, status, planned_date FROM roadmap_step "
                "WHERE roadmap_id = ?", (rid,),
            )
        }
        assert rows["prereqs"]["status"] == "skipped"
        assert rows["mcat"]["planned_date"] == "2026-11-15"
        assert "no-such-step" not in rows
        count = conn.execute(
            "SELECT t.weekly_count FROM roadmap_cadence c JOIN target t ON t.id = c.target_id "
            "WHERE c.roadmap_id = ? AND c.cadence_key = 'practice_sections'", (rid,),
        ).fetchone()["weekly_count"]
        assert count == 5

    def test_transcript_and_spend_are_persisted(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        client = ScriptedClient([QUESTIONS, ADJUSTMENTS])
        preset = presets.load("medical")
        result = interview.run_interview(
            conn, settings, client, preset, TODAY, ask=_answers
        )
        rid = instantiate.instantiate(conn, settings, preset, TODAY, result.adjustments)
        sid = interview.persist(conn, rid, result)

        item = conn.execute("SELECT * FROM source_item WHERE id = ?", (sid,)).fetchone()
        assert item["source"] == "roadmap-interview"
        assert item["extraction_version"] == "roadmap-interview-adjust@1"
        assert "MCAT" in item["body_text"]
        head = conn.execute("SELECT * FROM roadmap WHERE id = ?", (rid,)).fetchone()
        assert head["interview_source_item_id"] == sid
        assert head["personalized"] == 1
        run = conn.execute("SELECT * FROM run WHERE kind = 'interview'").fetchone()
        assert run is not None and run["spend_cents"] == 4  # 2 calls × $0.02

        # And the monthly cap sees interview spend: a cap equal to what was just spent
        # makes the next interview degrade immediately.
        settings2 = settings.model_copy(update={"monthly_spend_cap_cents": 4})
        again = interview.run_interview(
            conn, settings2, ScriptedClient([QUESTIONS, ADJUSTMENTS]),
            preset, TODAY, ask=_answers,
        )
        assert again.adjustments is None

    def test_call_ceiling_below_two_skips_the_interview(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        settings2 = settings.model_copy(update={"interview_max_calls": 1})
        client = ScriptedClient([QUESTIONS, ADJUSTMENTS])
        result = interview.run_interview(
            conn, settings2, client, presets.load("medical"), TODAY, ask=_answers
        )
        assert result.adjustments is None and client.calls == 0
