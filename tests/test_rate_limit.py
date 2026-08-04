"""A shut usage window is not a bad item, and must not be told as a spend cap.

Two failures live behind this file, and they pull in opposite directions.

The first: `--max-budget-usd` is gone and the monthly cap no longer enforces against the
subscription's imputed price, so a long run meets the subscription's own rolling usage
window instead of a dollar wall. That refusal used to arrive as a generic `ModelError`,
and sync parks an item after two attempts — turning a pause that clears in hours into a
permanent hole in the ledger. CLAUDE.md rule 5, in the direction that loses data.

The second is the fix's own failure mode, which is worse. A rate limit stops the whole
pass and consumes no attempt, so anything mistaken for one stalls the queue behind it on
every later sync while the panel promises a retry. The detection is therefore allowed to
match phrases a limit actually uses and nothing else — never a bare status number, which
a minified Node stack trace carries as a column offset.

And a pause has to say which pause it is. The cap releases on the first of the month and
leaves its items parked; a usage window releases in hours and leaves them pending. One
sentence cannot honestly be both.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.connectors.boundary import Boundary
from backglass.extract.client import ClaudeCLIBackend, ModelError, ModelResult, RateLimited
from backglass.sync import sync
from backglass.web.app import create_app
from tests.conftest import FakeModel, gmail_message, make_connector, panel_slice

SPECS: list[dict[str, Any]] = [
    {
        "id": "m1",
        "from": "Dana Whitfield <dwhitfield@example.gov>",
        "to": "alex.rivera@example.com",
        "subject": "Migration plan",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "I'll have the revised migration plan over to you by Friday.",
    },
]

EXTRACTIONS = {
    "Migration plan": {
        "commitments": [
            {
                "direction": "owed_to_me",
                "counterparty": "Dana Whitfield <dwhitfield@example.gov>",
                "what": "revised migration plan",
                "due_at": "2026-07-17",
                "due_is_explicit": True,
                "estimated_minutes": None,
                "confidence": 0.92,
                "evidence": "I'll have the revised migration plan over to you by Friday.",
                "resolves": False,
                "resolves_what": None,
            }
        ]
    },
}


@pytest.fixture
def messages() -> list[dict[str, Any]]:
    return [gmail_message(spec) for spec in SPECS]


class _LimitedOnce(FakeModel):
    """Refuses the named tier until `open_at`, then behaves like FakeModel.

    Modelled on the real shape: the window is shut for a while and then simply is not,
    with nothing about the item having changed.
    """

    def __init__(self, tier: str, *, open_at: int = 1, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tier = tier
        self.open_at = open_at
        self.refusals = 0

    def complete(self, **kwargs: Any) -> ModelResult:
        props = kwargs["schema"].get("properties", {})
        tier = (
            "triage" if "keep" in props else "triage_batch" if "items" in props else "extract"
        )
        if tier == self.tier and self.refusals < self.open_at:
            self.refusals += 1
            raise RateLimited("model rate limit: Claude usage limit reached")
        return super().complete(**kwargs)


class _AlwaysMalformed(FakeModel):
    """The other half of the contract: a response the model cannot get right."""

    def complete(self, **kwargs: Any) -> ModelResult:
        props = kwargs["schema"].get("properties", {})
        if "keep" not in props and "items" not in props:
            raise ModelError("model returned an unusable response after 2 attempts: bad JSON")
        return super().complete(**kwargs)


def _pending_extractions(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM source_item WHERE user_id = 1"
        " AND triage_verdict = 'keep' AND extraction_version IS NULL"
    ).fetchone()
    return int(row["n"])


# ── detection ─────────────────────────────────────────────────────────────


class _Completed:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _cli(monkeypatch: pytest.MonkeyPatch, completed: _Completed) -> list[list[str]]:
    """Run the CLI backend against a canned subprocess result; return the calls made."""
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> _Completed:
        calls.append(command)
        return completed

    monkeypatch.setattr("backglass.extract.client.subprocess.run", fake_run)
    return calls


class TestWhatCountsAsALimit:
    """Consulted only on a call the CLI already declared a failure."""

    def test_an_error_envelope_naming_a_usage_limit_is_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _cli(
            monkeypatch,
            _Completed(
                stdout='{"is_error": true, "subtype": "error_during_execution",'
                ' "result": "Claude AI usage limit reached", "total_cost_usd": 0.0}'
            ),
        )
        with pytest.raises(RateLimited):
            ClaudeCLIBackend().complete(
                system="s", user="u", schema={}, model="sonnet", budget_usd=0.1
            )
        assert len(calls) == 1, "a shut window must not consume the item's second attempt"

    def test_a_malformed_response_still_parks_after_two_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the contract. If everything came back pending, a poisoned
        item would be retried forever and the review queue would never see it."""
        calls = _cli(monkeypatch, _Completed(stdout='{"result": "not json at all"}'))
        with pytest.raises(ModelError) as raised:
            ClaudeCLIBackend().complete(
                system="s", user="u", schema={}, model="sonnet", budget_usd=0.1
            )
        assert not isinstance(raised.value, RateLimited)
        assert len(calls) == 2

    def test_a_schema_failure_that_quotes_the_words_still_parks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`error_max_structured_output_retries` is the model failing to produce the
        shape, which is exactly the retry-then-park case, however the text reads."""
        calls = _cli(
            monkeypatch,
            _Completed(
                stdout='{"is_error": true,'
                ' "subtype": "error_max_structured_output_retries",'
                ' "result": "gave up; the tool said something about a rate limit"}'
            ),
        )
        with pytest.raises(ModelError) as raised:
            ClaudeCLIBackend().complete(
                system="s", user="u", schema={}, model="sonnet", budget_usd=0.1
            )
        assert not isinstance(raised.value, RateLimited)
        assert len(calls) == 2

    def test_a_crashed_subprocess_whose_trace_carries_a_429_parks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The expensive mistake, and the reason no bare status number is a marker.

        A minified Node trace carries column offsets — `cli.js:1:429517`. Read as a limit
        it would break the pass and consume no attempt, so this deterministically crashing
        item would stall every item behind it on every later sync, forever, while the
        panel says they are pending and will be retried.
        """
        calls = _cli(
            monkeypatch,
            _Completed(
                stderr="TypeError: undefined is not a function\n"
                "    at wKe (/opt/claude/cli.js:1:429517)\n"
                "    at Object.<anonymous> (/opt/claude/cli.js:1:5291043)",
                returncode=1,
            ),
        )
        with pytest.raises(ModelError) as raised:
            ClaudeCLIBackend().complete(
                system="s", user="u", schema={}, model="sonnet", budget_usd=0.1
            )
        assert not isinstance(raised.value, RateLimited)
        assert len(calls) == 2, "a crash is the item's problem, so it spends its attempts"

    def test_a_crash_that_names_the_limit_is_still_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI can die before it writes an envelope, and the reason is then only on
        stderr. "usage limit reached" is unambiguous where a column offset is not."""
        calls = _cli(
            monkeypatch,
            _Completed(stderr="Claude AI usage limit reached. Try again later.", returncode=1),
        )
        with pytest.raises(RateLimited):
            ClaudeCLIBackend().complete(
                system="s", user="u", schema={}, model="sonnet", budget_usd=0.1
            )
        assert len(calls) == 1


# ── the sync ──────────────────────────────────────────────────────────────


class TestTheItemsSurvive:
    def test_a_rate_limited_extraction_leaves_the_item_pending_and_the_next_sync_gets_it(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        model = _LimitedOnce("extract", extract=EXTRACTIONS)

        first = sync(conn, settings, [make_connector(messages, boundary)], model)
        assert first.rate_limited
        assert first.degrade_reason == "rate_limit"
        assert first.extracted == 0
        assert first.parked == 0, "a shut window is not the item's fault; nothing is parked"
        assert _pending_extractions(conn) == 1

        second = sync(conn, settings, [make_connector(messages, boundary)], model)
        assert not second.rate_limited
        assert second.extracted == 1
        assert second.commitments_inserted == 1
        assert _pending_extractions(conn) == 0

    def test_a_malformed_extraction_still_parks(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        """Parked, surfaced, and NOT reported as a rate limit — the review queue and the
        error list are how a genuinely bad item becomes visible."""
        report = sync(conn, settings, [make_connector(messages, boundary)], _AlwaysMalformed())
        assert report.parked == 1
        assert not report.rate_limited
        assert report.degrade_reason is None
        assert any("extract" in e for e in report.errors)

    def test_a_rate_limited_triage_leaves_the_verdict_unwritten(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        model = _LimitedOnce("triage", extract=EXTRACTIONS)

        first = sync(conn, settings, [make_connector(messages, boundary)], model)
        assert first.rate_limited
        assert first.model_triaged == 0
        undecided = conn.execute(
            "SELECT COUNT(*) AS n FROM source_item WHERE triage_verdict IS NULL"
        ).fetchone()
        assert int(undecided["n"]) == 1

        second = sync(conn, settings, [make_connector(messages, boundary)], model)
        assert second.model_triaged == 1
        assert second.extracted == 1

    def test_the_batched_triage_path_recovers_too(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
    ) -> None:
        """Batched triage is a separate call site and a separate failure. Its items are
        deliberately not escalated to the per-item pass: that pass calls the same backend
        that just refused, so escalating would only re-fail every item one at a time."""
        many = [
            gmail_message(dict(SPECS[0], id=f"b{i}", subject=f"Migration plan {i}"))
            for i in range(4)
        ]
        batching = settings.model_copy(update={"triage_batch_min": 2})
        model = _LimitedOnce("triage_batch", extract=EXTRACTIONS)

        first = sync(conn, batching, [make_connector(many, boundary)], model)
        assert first.rate_limited
        assert first.escalated == 0
        assert first.model_triaged == 0

        second = sync(conn, batching, [make_connector(many, boundary)], model)
        assert not second.rate_limited
        assert second.model_triaged == 4

    def test_extraction_is_not_attempted_after_triage_hits_the_wall(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        """It would call the same backend that just refused, so the only product of the
        pass would be a longer error list."""
        model = _LimitedOnce("triage", extract=EXTRACTIONS)
        sync(conn, settings, [make_connector(messages, boundary)], model)
        assert not any(tier == "extract" for tier, _ in model.calls)

    def test_the_recovered_run_is_still_idempotent(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        """CLAUDE.md rule 3. A retry path that re-reads items must not re-write them."""
        model = _LimitedOnce("extract", extract=EXTRACTIONS)
        sync(conn, settings, [make_connector(messages, boundary)], model)
        sync(conn, settings, [make_connector(messages, boundary)], model)
        third = sync(conn, settings, [make_connector(messages, boundary)], model)
        assert third.writes == 0


class TestTheRunSaysWhichPause:
    def test_the_reason_is_persisted(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        sync(
            conn,
            settings,
            [make_connector(messages, boundary)],
            _LimitedOnce("extract", extract=EXTRACTIONS),
        )
        row = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
        assert row["degraded"] == 1
        assert row["degrade_reason"] == "rate_limit"

    def test_the_cap_keeps_its_own_reason(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """A billed backend over its cap is still the cap, and still says so."""
        conn.execute(
            "INSERT INTO run (user_id, started_at, spend_cents) VALUES (1, ?, 9999)",
            ("2026-08-03T00:00:00+00:00",),
        )
        capped = settings.model_copy(update={"monthly_spend_cap_cents": 100})
        report = sync(conn, capped, [], FakeModel())
        assert report.degrade_reason == "spend_cap"


# ── the surfaces ──────────────────────────────────────────────────────────


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn  # migrated db on disk; the app opens its own connections
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _degraded_run(conn: sqlite3.Connection, reason: str | None) -> None:
    conn.execute(
        "INSERT INTO run (user_id, started_at, finished_at, degraded, degrade_reason)"
        " VALUES (1, '2026-08-03T05:00:00+00:00', '2026-08-03T05:01:00+00:00', 1, ?)",
        (reason,),
    )
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " content_hash, triage_verdict) VALUES (1, 'gmail:personal', 's1',"
        " '2026-08-03T08:00:00Z', '2026-08-03T08:00:00Z', 'h1', 'keep')"
    )
    conn.commit()


class TestNeitherPauseClaimsTheOthersCause:
    def test_the_panel_says_pending_and_retried_not_capped(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _degraded_run(conn, "rate_limit")
        panel = panel_slice(client.get("/").text, "panel-sources")
        assert "Model rate limit reached — extraction paused for that run" in panel
        assert "the next scheduled sync retries them" in panel
        # A pause that clears in hours must not hand the owner a month-end date.
        assert "Spend cap" not in panel
        assert "the cap resets" not in panel

    def test_the_panel_still_says_the_cap_when_it_was_the_cap(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _degraded_run(conn, "spend_cap")
        panel = panel_slice(client.get("/").text, "panel-sources")
        assert "Spend cap reached — extraction paused, triage only." in panel
        assert "the cap resets" in panel
        assert "rate limit" not in panel

    def test_a_pre_migration_row_reads_as_the_cap(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """NULL is not a third state: before 0016 the cap was the only thing that could
        pause a run, so that is what a NULL degraded row means."""
        _degraded_run(conn, None)
        panel = panel_slice(client.get("/").text, "panel-sources")
        assert "Spend cap reached" in panel

    def test_the_sidebar_alert_carries_the_same_sentence(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _degraded_run(conn, "rate_limit")
        page = client.get("/goals").text  # a page with no Sources panel on it
        assert "the next scheduled sync retries them" in page
        assert "the cap resets" not in page

    def test_the_brief_says_it_too(self, conn: sqlite3.Connection, settings: Settings) -> None:
        from backglass.brief import daily

        _degraded_run(conn, "rate_limit")
        brief = daily.build(conn, settings, __import__("datetime").date(2026, 8, 3))
        lines = [line.text for line in brief.all_lines()]
        assert any("Model rate limit reached" in text for text in lines)
        assert not any("Spend cap" in text for text in lines)

    def test_the_cli_prints_the_pending_sentence(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from backglass.__main__ import _print_report
        from backglass.sync import SyncReport

        _print_report(
            SyncReport(degraded=True, degrade_reason="rate_limit"), dry_run=False
        )
        err = capsys.readouterr().err
        assert "model rate limit reached" in err
        assert "the next sync retries them" in err
        assert "spend cap" not in err
