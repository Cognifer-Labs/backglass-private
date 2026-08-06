"""A cap on money must not be enforced against a price nobody is charged.

`MODEL_BACKEND=claude_cli` runs on the owner's flat-rate subscription. The CLI still
reports `total_cost_usd` — what the same call would have cost on the API — and `SpendCap`
enforced that as a bill. On 2026-08-03 nine consecutive syncs degraded to triage-only at
2006c of an unbilled 2000c cap, stranding every kept item behind a charge that did not
exist. The number is worth recording; it is not worth stopping work over.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Any

import pytest

from backglass import costs
from backglass.config import Settings
from backglass.extract.client import (
    AnthropicAPIBackend,
    ClaudeCLIBackend,
    DeepInfraBackend,
    ModelResult,
    TriageRouter,
    spend_is_imputed,
)
from backglass.sync import SpendCap

TODAY = date(2026, 8, 10)


class _Billed:
    spend_is_imputed = False

    def complete(self, **_: Any) -> ModelResult:  # pragma: no cover - never called
        raise AssertionError


class _Subscription:
    spend_is_imputed = True

    def complete(self, **_: Any) -> ModelResult:  # pragma: no cover - never called
        raise AssertionError


def _spend(conn: sqlite3.Connection, cents: int) -> None:
    conn.execute(
        "INSERT INTO run (user_id, started_at, spend_cents, items_fetched,"
        " items_triaged_out, items_extracted, writes, degraded)"
        " VALUES (1, ?, ?, 0, 0, 0, 0, 0)",
        (datetime.now(UTC).isoformat(), cents),
    )


class TestTheCapKnowsWhatItIsCapping:
    def test_imputed_spend_never_reaches_the_cap(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The exact 2026-08-03 state: over the cap, and nothing should stop."""
        _spend(conn, 2006)
        capped = settings.model_copy(update={"monthly_spend_cap_cents": 2000})
        cap = SpendCap(conn, capped, _Subscription())
        assert cap.total_cents == 2006
        assert not cap.reached

    def test_billed_spend_still_reaches_the_cap(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _spend(conn, 2006)
        capped = settings.model_copy(update={"monthly_spend_cap_cents": 2000})
        assert SpendCap(conn, capped, _Billed()).reached

    def test_a_caller_that_does_not_say_is_treated_as_billed(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The safe direction for a money guard is on.

        `batch.py` constructs a cap without a client and runs against the Batches API,
        which bills for real. A default of "imputed" would silently disarm it.
        """
        _spend(conn, 2006)
        capped = settings.model_copy(update={"monthly_spend_cap_cents": 2000})
        assert SpendCap(conn, capped).reached

    def test_imputed_spend_is_still_charged_and_reported(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Not enforcing is not the same as not recording."""
        cap = SpendCap(conn, settings, _Subscription())
        cap.charge(0.50)
        assert cap.total_cents == 50


class TestBackendsDeclareThemselves:
    @pytest.mark.parametrize(
        ("backend", "imputed"),
        [
            (ClaudeCLIBackend, True),
            (DeepInfraBackend, False),
            (AnthropicAPIBackend, False),
        ],
    )
    def test_each_backend_answers(self, backend: type, imputed: bool) -> None:
        assert backend.spend_is_imputed is imputed

    @pytest.mark.parametrize(
        ("name", "imputed"),
        [("claude_cli", True), ("deepinfra", False), ("anthropic", False)],
    )
    def test_settings_resolve_to_the_same_answer(
        self, settings: Settings, name: str, imputed: bool
    ) -> None:
        """`spend_is_imputed(settings)` must agree with what `build` would return —
        it is read off the same classes precisely so the two cannot drift."""
        assert spend_is_imputed(settings.model_copy(update={"model_backend": name})) is imputed

    def test_the_router_answers_for_its_primary(self) -> None:
        """Apple triage is free either way; the cap question is about extraction."""
        from backglass.extract.client import AppleShortcutBackend

        router = TriageRouter(primary=ClaudeCLIBackend(), triage=AppleShortcutBackend())
        assert router.spend_is_imputed is True
        paid = TriageRouter(
            primary=AnthropicAPIBackend(api_key="x"), triage=AppleShortcutBackend()
        )
        assert paid.spend_is_imputed is False


class TestTheCLICommand:
    def test_no_budget_flag_is_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`--max-budget-usd` is a ceiling against the same imputed price.

        Measured 2026-08-03: a trivial prompt reported ~4c, dominated by the CLI's own
        session `cache_creation_input_tokens` rather than by the payload. A per-call
        ceiling of $0.10 against that number aborts real work over imaginary money.
        """
        seen: dict[str, Any] = {}

        class _Completed:
            returncode = 0
            stdout = '{"structured_output": {"keep": true}, "total_cost_usd": 0.04}'
            stderr = ""

        def fake_run(command: list[str], **kwargs: Any) -> Any:
            seen["command"] = command
            return _Completed()

        monkeypatch.setattr("backglass.extract.client.subprocess.run", fake_run)
        result = ClaudeCLIBackend(executable="claude").complete(
            system="s", user="u", schema={"properties": {"keep": {}}},
            model="haiku", budget_usd=0.10,
        )
        assert result.data == {"keep": True}
        assert "--max-budget-usd" not in seen["command"]


class TestTheReportSaysWhichKindOfNumber:
    def test_month_carries_the_distinction(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        conn.execute(
            "INSERT INTO run (user_id, started_at, spend_cents, items_fetched,"
            " items_triaged_out, items_extracted, writes, degraded)"
            " VALUES (1, '2026-08-02T00:00:00+00:00', 1900, 10, 5, 5, 5, 0)"
        )
        subscription = settings.model_copy(update={"model_backend": "claude_cli"})
        assert costs.month(conn, subscription, TODAY).spend_is_imputed is True
        billed = settings.model_copy(update={"model_backend": "anthropic"})
        assert costs.month(conn, billed, TODAY).spend_is_imputed is False
