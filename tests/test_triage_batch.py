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
                "to": "alex.rivera@example.com",
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
        # rides in `system` for caching), not the batch excerpt prompt. The dynamic half
        # begins at `{{owner_context}}` since triage v3 — the instruction block is still
        # static and still cached; what moved across the boundary is the literal MESSAGE
        # header, because split() cuts before the FIRST placeholder and that is now the
        # context. The assertion checks the item is carried in the dynamic half, which
        # is what it was always for.
        for tier, system, user in model.users:
            if tier == "triage":
                assert "You are triaging one message" in system
                assert "MESSAGE\nFrom: " in user

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


# ── packing batches by size rather than by count ────────────────────────────


class TestPacking:
    def _items(self, count: int, length: int) -> list[dict[str, object]]:
        return [{"id": i, "body_text": "x" * length} for i in range(count)]

    def test_short_items_pack_denser_than_long_ones(self, settings: Settings) -> None:
        """The constraint on a batch is the context it fits into, not how many things are
        in it. A mail runs to the 500-character excerpt ceiling while an iMessage averages
        twenty-five, so packing both twelve at a time made the owner's 3,687 messages cost
        308 calls where 55 would do."""
        from backglass.extract.triage import BATCH_BODY_LIMIT
        from backglass.sync import _pack

        long_batches = _pack(self._items(60, BATCH_BODY_LIMIT), settings)
        short_batches = _pack(self._items(60, 20), settings)

        assert len(short_batches) < len(long_batches)

    def test_mail_shaped_items_keep_the_old_batch_size(self, settings: Settings) -> None:
        """`triage_batch_size` still sets the floor through the character budget, so
        nothing changes for items that were already filling a batch."""
        from backglass.extract.triage import BATCH_BODY_LIMIT
        from backglass.sync import _pack

        batches = _pack(self._items(36, BATCH_BODY_LIMIT), settings)

        assert [len(b) for b in batches] == [12, 12, 12]

    def test_no_batch_exceeds_the_item_ceiling(self, settings: Settings) -> None:
        """A run of one-word texts would otherwise build a batch so long the model stops
        aligning its verdict list to the ids. That degrades safely — missing ids escalate
        to per-item — but paying for an extra call beats relying on the fallback."""
        from backglass.sync import _pack

        batches = _pack(self._items(500, 1), settings)

        assert batches, "everything is still batched"
        assert max(len(b) for b in batches) <= settings.triage_batch_max_items

    def test_every_item_lands_in_exactly_one_batch(self, settings: Settings) -> None:
        """The property that matters most: packing must not drop or duplicate work."""
        from backglass.sync import _pack

        items = self._items(137, 30)
        packed = [item["id"] for batch in _pack(items, settings) for item in batch]

        assert sorted(packed) == [i["id"] for i in items]

    def test_an_item_longer_than_the_budget_still_gets_a_batch(
        self, settings: Settings
    ) -> None:
        """Cost is clamped to the excerpt ceiling, so one enormous body cannot starve the
        packer into an empty batch or an infinite loop."""
        from backglass.sync import _pack

        batches = _pack(self._items(3, 50_000), settings)

        assert sum(len(b) for b in batches) == 3
