"""Batched triage: one call per ~dozen items, and the escalation contract.

The precision guarantee under test: nothing is ever dropped on a 500-char excerpt
unless the model affirmatively, confidently dropped it. Uncertain, missing,
disagreeing, and failed-batch items are all re-read per-item in full.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from backglass.config import Settings
from backglass.sync import sync
from tests.conftest import FakeModel, ModelResult, gmail_message, make_connector

PROSE = [
    "The quarterly numbers moved again and finance wants a narrative.",
    "Our vendor renewal lands next month and needs a decision from you.",
    "The migration cutover window shifted; ops will confirm the date.",
    "Recruiting wants your read on the two staff candidates.",
    "Legal returned the redlines; two clauses still open.",
    "The beta cohort feedback is in and mostly positive.",
    "Infra costs crossed the alert threshold on Tuesday.",
    "The conference talk was accepted; slides due at some point.",
    "A customer asked about the export API roadmap.",
    "The design review raised a question about offline mode.",
    "Support saw a spike in login failures over the weekend.",
    "The board pre-read needs one more chart on retention.",
]


def _messages(n: int) -> list[dict[str, Any]]:
    return [
        gmail_message(
            {
                "id": f"b{i}",
                "from": f"sender{i}@corp{i}.example",
                "to": "contactdharsan@gmail.com",
                "subject": f"Update {chr(65 + i)}",
                "date": f"Fri, {10 + i:02d} Jul 2026 09:00:00 -0700",
                "body": PROSE[i % len(PROSE)],
            }
        )
        for i in range(n)
    ]


class RecordingModel(FakeModel):
    """FakeModel that also keeps every rendered (system, user) pair, per tier."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.users: list[tuple[str, str, str]] = []

    def complete(self, *, system: str, user: str, schema: dict[str, Any], model: str,
                 budget_usd: float) -> ModelResult:
        result = super().complete(
            system=system, user=user, schema=schema, model=model, budget_usd=budget_usd
        )
        self.users.append((self.calls[-1][0], system, user))
        return result


def _tiers(model: FakeModel) -> list[str]:
    return [tier for tier, _ in model.calls]


class TestHappyPath:
    def test_twelve_items_cost_one_call(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        batch_response = {
            "items": [
                {"id": i, "keep": False, "reason": "status chatter, no ask"}
                for i in range(1, 13)
            ]
        }
        model = FakeModel(triage_batch={"--- id: 1": batch_response})
        report = sync(conn, settings, [make_connector(_messages(12), boundary)], model)

        assert _tiers(model) == ["triage_batch"]
        assert report.model_triaged == 12
        assert report.batched == 12
        assert report.escalated == 0
        assert report.triaged_out == 12
        assert report.spend_cents >= 0
        verdicts = {
            r["triage_verdict"]
            for r in conn.execute("SELECT triage_verdict FROM source_item")
        }
        assert verdicts == {"drop"}

    def test_below_threshold_stays_per_item(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        model = FakeModel()
        sync(conn, settings, [make_connector(_messages(3), boundary)], model)
        assert "triage_batch" not in _tiers(model)


class TestEscalation:
    def test_uncertain_and_missing_ids_are_reread_in_full(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        # 12 sent: 9 confident drops, 2 uncertain, 1 (id 12) missing entirely.
        batch_response = {
            "items": [
                {"id": i, "keep": False, "reason": "chatter"} for i in range(1, 10)
            ]
            + [
                {"id": 10, "keep": False, "reason": "cannot tell", "uncertain": True},
                {"id": 11, "keep": True, "reason": "maybe an ask", "uncertain": True},
            ]
        }
        model = RecordingModel(triage_batch={"--- id: 1": batch_response})
        report = sync(conn, settings, [make_connector(_messages(12), boundary)], model)

        assert _tiers(model).count("triage_batch") == 1
        assert _tiers(model).count("triage") == 3, "2 uncertain + 1 missing"
        assert report.batched == 9
        assert report.escalated == 3
        assert report.model_triaged == 12
        # The per-item pass must run the full single-item prompt (its static half now
        # rides in `system` for caching), not the batch excerpt prompt.
        for tier, system, user in model.users:
            if tier == "triage":
                assert "You are triaging one message" in system
                assert user.startswith("From: ")

    def test_unknown_ids_in_the_response_are_ignored(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        batch_response = {
            "items": [
                {"id": i, "keep": False, "reason": "chatter"} for i in range(1, 5)
            ]
            + [{"id": 999, "keep": False, "reason": "hallucinated"}]
        }
        model = FakeModel(triage_batch={"--- id: 1": batch_response})
        sync(conn, settings, [make_connector(_messages(4), boundary)], model)
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM source_item WHERE triage_verdict IS NOT NULL"
        ).fetchone()["n"]
        assert n == 4, "999 never existed and never will"

    def test_failed_batch_escalates_everything_and_does_not_block(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        model = FakeModel(triage_batch={"--- id: 1": {"garbage": True}})
        report = sync(conn, settings, [make_connector(_messages(5), boundary)], model)

        assert _tiers(model).count("triage_batch") == 1
        assert _tiers(model).count("triage") == 5
        assert report.escalated == 5
        assert report.model_triaged == 5
        assert report.exit_code == 0, "a failed batch is a fallback, not a failure"


class TestIdempotency:
    def test_second_batched_run_writes_and_calls_nothing(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        batch_response = {
            "items": [
                {"id": i, "keep": False, "reason": "chatter"} for i in range(1, 13)
            ]
        }
        model = FakeModel(triage_batch={"--- id: 1": batch_response})
        sync(conn, settings, [make_connector(_messages(12), boundary)], model)
        calls = len(model.calls)

        second = sync(conn, settings, [make_connector(_messages(12), boundary)], model)
        assert second.writes == 0
        assert len(model.calls) == calls
