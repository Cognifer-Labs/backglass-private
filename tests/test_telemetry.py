"""One row per model call — Phase 0 of the backend plan.

Driven through `sync` rather than through `Metered` directly. A wrapper tested on its
own proves it can count; what has to be true is that the real passes are wrapped, that
the tier they are wrapped with is the tier they are, and that the rows survive the run
row being written after them.
"""

from __future__ import annotations

import sqlite3

import pytest

from backglass.config import Settings
from backglass.extract.client import ModelError, RateLimited
from backglass.sync import sync
from backglass.telemetry import CallRecord, Metered, write_calls
from tests.conftest import FakeModel, gmail_message, make_connector


def _mail(n: int) -> list[dict[str, object]]:
    return [
        gmail_message(
            {
                "id": f"m{i}",
                "from": "someone@example.org",
                "to": "alex.rivera@example.com",
                "subject": f"Subject {i}",
                "date": "Fri, 31 Jul 2026 09:00:00 -0700",
                "body": "Can you send the report by Friday?",
            }
        )
        for i in range(n)
    ]


def _calls(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM model_call ORDER BY id"))


class TestRecordedThroughSync:
    def test_a_triage_call_is_recorded_with_its_tier(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        model = FakeModel({"verdict": "drop", "reason": "no ask"})
        sync(conn, settings, [make_connector(_mail(1), boundary)], model)
        rows = _calls(conn)
        # Triage first, and extraction after it on the same item — the fake answers
        # both passes, which is what makes this assert the tiers are told apart rather
        # than that one of them exists.
        assert [r["tier"] for r in rows] == ["triage", "extract"]
        assert {r["outcome"] for r in rows} == {"ok"}
        # The payload is sized including the system half: on the CLI backend it is
        # re-sent in a fresh process every call, which is the thing being measured.
        assert rows[0]["prompt_chars"] > 0
        assert rows[0]["duration_ms"] >= 0

    def test_every_call_carries_the_run_that_made_it(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """The run row is written after the calls, so the run stamps them. If that
        ordering ever inverts, every row keeps a NULL run_id and the readout silently
        loses its grouping."""
        model = FakeModel({"verdict": "drop", "reason": "no ask"})
        sync(conn, settings, [make_connector(_mail(2), boundary)], model)
        run_id = conn.execute("SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()["id"]
        rows = _calls(conn)
        assert rows, "the run made calls"
        assert {r["run_id"] for r in rows} == {run_id}

    def test_the_batch_pass_is_recorded_as_its_own_tier(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """Batched triage and per-item triage are the two shapes the plan compares, so
        they cannot share a label — the whole question is what one costs against the
        other."""
        batching = settings.model_copy(update={"triage_batch_min": 2, "triage_batch_size": 4})
        model = FakeModel({"verdicts": [], "reason": "batch"})
        sync(conn, batching, [make_connector(_mail(4), boundary)], model)
        assert "triage_batch" in {r["tier"] for r in _calls(conn)}

    def test_a_failed_call_is_recorded_too(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """A call that failed still took time and may still have cost something. A
        telemetry table that only holds successes reports a pipeline faster and cheaper
        than the one that ran."""

        class Failing(FakeModel):
            def complete(self, **kwargs: object) -> object:
                raise ModelError("model returned an unusable response")

        sync(conn, settings, [make_connector(_mail(1), boundary)], Failing({}))
        rows = _calls(conn)
        assert rows, "the failure was recorded"
        assert rows[0]["outcome"] == "error"


class TestTheWrapperItself:
    def test_a_rate_limit_is_labelled_as_one(self) -> None:
        """Rate-limited and error are different rows on purpose: one is a shut window
        that costs nothing to retry later, the other is work that will fail again."""

        class Limited:
            spend_is_imputed = True

            def complete(self, **kwargs: object) -> object:
                exc = RateLimited("window shut")
                exc.cost_usd = 0.02  # type: ignore[attr-defined]
                raise exc

        sink: list[CallRecord] = []
        metered = Metered(Limited(), "extract", sink)  # type: ignore[arg-type]
        with pytest.raises(RateLimited):
            metered.complete(system="s", user="u", schema={}, model="sonnet", budget_usd=1.0)
        assert [(c.tier, c.outcome, c.cost_usd) for c in sink] == [
            ("extract", "rate_limited", 0.02)
        ]

    def test_the_wrapper_does_not_answer_for_the_backend_about_money(self) -> None:
        """SpendCap reads `spend_is_imputed` off whatever client it is handed. A wrapper
        that answered False for a subscription backend would make the cap hard against
        money nobody is charged — the 2026-08-03 freeze, reinstated by a decorator."""

        class Subscription:
            spend_is_imputed = True

            def complete(self, **kwargs: object) -> object:
                raise AssertionError("not called")

        assert Metered(Subscription(), "triage", []).spend_is_imputed is True  # type: ignore[arg-type]

    def test_calls_are_written_unstamped_when_the_run_never_finished(
        self, conn: sqlite3.Connection
    ) -> None:
        """`run_id` is nullable for the crash between the last call and the run row. The
        row is still the truth about a call that happened."""
        written = write_calls(
            conn,
            [CallRecord("extract", "sonnet", 120, 900, 0.01, "ok", "2026-08-06T09:00:00Z")],
            None,
        )
        assert written == 1
        assert conn.execute("SELECT run_id FROM model_call").fetchone()["run_id"] is None
