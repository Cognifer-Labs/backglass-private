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
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.connectors.boundary import Boundary
from backglass.db import now_iso
from backglass.extract.client import ClaudeCLIBackend, ModelError, ModelResult, RateLimited
from backglass.sync import sync
from backglass.web.app import create_app
from tests.conftest import FakeModel, gmail_message, make_connector, panel_slice

# Two messages, not one, and that is load-bearing: a single item claiming a rate limit is
# a claim nothing corroborates, and sync now refuses to stop a pass on one. A real window
# is shut for every call, so the fixtures below refuse every call in the first run.
SPECS: list[dict[str, Any]] = [
    {
        "id": "m1",
        "from": "Dana Whitfield <dwhitfield@example.gov>",
        "to": "alex.rivera@example.com",
        "subject": "Migration plan",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "I'll have the revised migration plan over to you by Friday.",
    },
    {
        "id": "m2",
        "from": "Priya Raman <praman@example.org>",
        "to": "alex.rivera@example.com",
        "subject": "Vendor contract",
        "date": "Fri, 10 Jul 2026 11:40:00 -0700",
        "body": "I'll send the signed vendor contract across by Wednesday.",
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
    "Vendor contract": {
        "commitments": [
            {
                "direction": "owed_to_me",
                "counterparty": "Priya Raman <praman@example.org>",
                "what": "signed vendor contract",
                "due_at": "2026-07-15",
                "due_is_explicit": True,
                "estimated_minutes": None,
                "confidence": 0.9,
                "evidence": "I'll send the signed vendor contract across by Wednesday.",
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
    """Refuses the named tier until `open_at` refusals, then behaves like FakeModel.

    Modelled on the real shape: the window is shut for a while and then simply is not,
    with nothing about the item having changed. `open_at` is the number of calls the
    stopped run makes, so the window is shut for all of them — which is what makes the
    second claimant sync requires appear.
    """

    def __init__(self, tier: str, *, open_at: int = 2, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tier = tier
        self.open_at = open_at
        self.refusals = 0
        # A wave runs its calls in threads, and a test that asserts "three of the five
        # siblings got through" needs the count of refusals to be exactly the count.
        self._lock = threading.Lock()

    def complete(self, **kwargs: Any) -> ModelResult:
        props = kwargs["schema"].get("properties", {})
        tier = (
            "triage" if "keep" in props else "triage_batch" if "items" in props else "extract"
        )
        with self._lock:
            refuse = tier == self.tier and self.refusals < self.open_at
            if refuse:
                self.refusals += 1
        if refuse:
            raise RateLimited("model rate limit: Claude usage limit reached")
        return super().complete(**kwargs)


class _ShutForOne(FakeModel):
    """One item, and only ever that item, claims a limit on extraction.

    The shape a false positive takes. A real window is shut for every call; a single item
    saying so on every run is a deterministic failure that happened to read like a limit,
    and it is the case that used to stall every item behind it forever.
    """

    def __init__(self, marker: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.marker = marker

    def complete(self, **kwargs: Any) -> ModelResult:
        props = kwargs["schema"].get("properties", {})
        extracting = "keep" not in props and "items" not in props
        if extracting and self.marker in kwargs["user"]:
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

    #: Every label the CLI's limit table can render, in the order it defines them. Four of
    #: the six carry no phrase from the marker list — including both per-model weekly
    #: limits, which are the ones a pipeline that pins `model_triage`/`model_extract` is
    #: most likely to meet, and "usage credit limit", which does not contain "usage limit".
    #: They reach the owner only through the wrapper, so the wrapper is what is matched.
    CLI_LIMIT_LABELS = [
        "session limit",  # five_hour
        "weekly limit",  # seven_day
        "Opus limit",  # seven_day_opus
        "Sonnet limit",  # seven_day_sonnet
        "Fable 5 limit",  # seven_day_overage_included
        "usage credit limit",  # overage
    ]

    @pytest.mark.parametrize("label", CLI_LIMIT_LABELS)
    def test_every_limit_the_cli_can_name_is_read_as_one(
        self, monkeypatch: pytest.MonkeyPatch, label: str
    ) -> None:
        """A marker list narrower than the binary it was read from is a false negative,
        and a false negative here is the original bug: the item parks after two attempts
        and a pause that clears in hours becomes a permanent hole in the ledger."""
        calls = _cli(
            monkeypatch,
            _Completed(stderr=f"You've hit your {label} · resets 6:00pm", returncode=1),
        )
        with pytest.raises(RateLimited):
            ClaudeCLIBackend().complete(
                system="s", user="u", schema={}, model="sonnet", budget_usd=0.1
            )
        assert len(calls) == 1

    def test_a_model_name_that_does_not_exist_yet_is_covered_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wrapper is matched rather than the six labels because the labels are the
        part that changes: a seventh model ships a seventh label, and a list that has to
        be revised on every release is silently wrong between them."""
        _cli(monkeypatch, _Completed(stderr="You've hit your Haiku 7 limit", returncode=1))
        with pytest.raises(RateLimited):
            ClaudeCLIBackend().complete(
                system="s", user="u", schema={}, model="sonnet", budget_usd=0.1
            )

    def test_the_wrapper_does_not_meet_across_a_paragraph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bounded on purpose. Two common words that match wherever they both appear are
        the same mistake as a bare status number, in slower motion."""
        calls = _cli(
            monkeypatch,
            _Completed(
                stderr="could not hit your endpoint: " + "x" * 120 + " recursion limit",
                returncode=1,
            ),
        )
        with pytest.raises(ModelError) as raised:
            ClaudeCLIBackend().complete(
                system="s", user="u", schema={}, model="sonnet", budget_usd=0.1
            )
        assert not isinstance(raised.value, RateLimited)
        assert len(calls) == 2

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
        assert first.degrade_reason == "rate_limit:extract"
        assert first.extracted == 0
        assert first.parked == 0, "a shut window is not the item's fault; nothing is parked"
        assert _pending_extractions(conn) == 2

        second = sync(conn, settings, [make_connector(messages, boundary)], model)
        assert not second.rate_limited
        assert second.extracted == 2
        assert second.commitments_inserted == 2
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
        assert report.parked == 2
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
        assert first.degrade_reason == "rate_limit:triage"
        assert first.model_triaged == 0
        undecided = conn.execute(
            "SELECT COUNT(*) AS n FROM source_item WHERE triage_verdict IS NULL"
        ).fetchone()
        assert int(undecided["n"]) == 2

        second = sync(conn, settings, [make_connector(messages, boundary)], model)
        assert second.model_triaged == 2
        assert second.extracted == 2

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
        # Two chunks, because one chunk is one claimant and sync does not stop a pass on
        # a single one. `triage_batch_max_items` floors at 2, so 4 items pack into 2.
        batching = settings.model_copy(
            update={"triage_batch_min": 2, "triage_batch_max_items": 1}
        )
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


    def test_a_triage_limit_then_a_recovery_is_idempotent_too(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        """The other stage, and the harder one: nothing was written on the stopped run, so
        the recovered run writes everything, and the run after it must write nothing."""
        model = _LimitedOnce("triage", extract=EXTRACTIONS)
        for _ in range(2):
            sync(conn, settings, [make_connector(messages, boundary)], model)
        assert sync(conn, settings, [make_connector(messages, boundary)], model).writes == 0

    def test_a_partly_completed_wave_is_idempotent_after_it_recovers(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
    ) -> None:
        """The wave that keeps its finished siblings writes some of its items and not
        others. Rule 3 is about the second run of an unchanged upstream, and a partial
        first run is exactly where a re-read could turn into a re-write."""
        many = [
            gmail_message(
                dict(
                    SPECS[0],
                    id=f"i{i}",
                    subject=f"Migration plan {i}",
                    date=f"Fri, 10 Jul 2026 0{i}:15:00 -0700",
                )
            )
            for i in range(8)
        ]
        parallel = settings.model_copy(
            update={"max_concurrency": 5, "triage_batch_min": 99}
        )
        model = _LimitedOnce("extract", open_at=2, extract=EXTRACTIONS)

        first = sync(conn, parallel, [make_connector(many, boundary)], model)
        assert first.rate_limited and first.extracted == 3
        sync(conn, parallel, [make_connector(many, boundary)], model)
        assert sync(conn, parallel, [make_connector(many, boundary)], model).writes == 0


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
        assert row["degrade_reason"] == "rate_limit:extract"

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


class TestOneClaimIsNotAWall:
    """A stopped wave is cheap when the window is shut and ruinous when it is not.

    It consumes no attempt, so anything mistaken for a limit stalls every item behind it
    on every later sync — forever — while the panel promises a retry. Detection is phrases
    only in order to make that unreachable, but the blast radius is a silent permanent
    stall under a reassuring sentence, which is the exact failure this change exists to
    remove, so it does not rest on detection being perfect.
    """

    def test_a_lone_claimant_parks_and_the_rest_of_the_pass_continues(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        model = _ShutForOne("Vendor contract", extract=EXTRACTIONS)

        report = sync(conn, settings, [make_connector(messages, boundary)], model)

        assert not report.rate_limited, "one claimant is a claim, not a closed window"
        assert report.degrade_reason is None
        assert report.extracted == 1, "the sibling must not be stalled behind it"
        assert report.parked == 1
        assert any("extract" in e for e in report.errors), "and it is visible, not silent"

    def test_the_stall_does_not_survive_into_later_runs(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        """The unbounded shape: a deterministic poison item claiming a limit every run,
        stopping the pass every run, with the ledger permanently short and every surface
        saying the next sync will fix it."""
        model = _ShutForOne("Vendor contract", extract=EXTRACTIONS)
        conns = [make_connector(messages, boundary) for _ in range(3)]

        reports = [sync(conn, settings, [c], model) for c in conns]

        assert [r.extracted for r in reports] == [1, 0, 0]
        assert not any(r.rate_limited for r in reports)
        assert all(r.parked == 1 for r in reports), "parked and surfaced on every run"

    def test_a_second_claimant_still_stops_the_wave(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
        messages: list[dict[str, Any]],
    ) -> None:
        """The genuine case is not weakened: a real window is shut for every call, so the
        second claimant always arrives — here on the second item of a serial pass."""
        model = _LimitedOnce("extract", open_at=2, extract=EXTRACTIONS)

        report = sync(conn, settings, [make_connector(messages, boundary)], model)

        assert report.rate_limited
        assert report.parked == 0, "neither claimant spends an attempt"
        assert _pending_extractions(conn) == 2


class TestWorkAlreadyPaidForIsKept:
    def test_siblings_that_finished_before_the_limit_are_extracted_and_charged(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
    ) -> None:
        """Breaking out of the loop stopped the wave the run was already inside.

        Those calls were submitted before the limit landed. They ran, they spent quota out
        of the very window being backed off from, and their results were discarded — so
        the run extracted nothing, `run.spend_cents` reported nothing, and the next sync
        paid for the same eight items again. Stopping means starting nothing new, not
        throwing away what is already bought.
        """
        many = [
            gmail_message(
                dict(
                    SPECS[0],
                    id=f"p{i}",
                    subject=f"Migration plan {i}",
                    date=f"Fri, 10 Jul 2026 0{i}:15:00 -0700",
                )
            )
            for i in range(8)
        ]
        # Five at a time, so the wave that meets the wall has siblings in flight; the
        # per-item triage path keeps the call count readable.
        parallel = settings.model_copy(
            update={"max_concurrency": 5, "triage_batch_min": 99}
        )
        model = _LimitedOnce("extract", open_at=2, extract=EXTRACTIONS, cost_usd=0.02)

        report = sync(conn, parallel, [make_connector(many, boundary)], model)

        # `calls` records only the calls that returned; `refusals` counts the rest.
        extract_calls = [c for c in model.calls if c[0] == "extract"]
        assert report.rate_limited
        assert len(extract_calls) + model.refusals == 5, (
            "one wave of five ran, and no wave was submitted after it"
        )
        assert report.extracted == 3, "the three that returned before the wall are kept"
        # 8 triage calls plus the 3 extractions that landed, at 2c each. The refused calls
        # cost nothing here; what must not happen is the successes costing nothing either.
        assert report.spend_cents == 22
        assert _pending_extractions(conn) == 5

        run = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
        assert int(run["spend_cents"]) == 22
        assert int(run["items_extracted"]) == 3

    def test_a_single_batch_claiming_a_limit_escalates_rather_than_stopping_triage(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        boundary: Boundary,
    ) -> None:
        """One chunk is one claimant, so it takes the ordinary failed-batch path: every
        item re-read per-item, which is exactly what escalation is for. A genuinely shut
        window then meets the same bound one level down, on the per-item calls."""
        many = [
            gmail_message(dict(SPECS[0], id=f"b{i}", subject=f"Migration plan {i}"))
            for i in range(4)
        ]
        batching = settings.model_copy(update={"triage_batch_min": 2})
        model = _LimitedOnce("triage_batch", open_at=1, extract=EXTRACTIONS)

        report = sync(conn, batching, [make_connector(many, boundary)], model)

        assert not report.rate_limited
        assert report.escalated == 4
        assert report.model_triaged == 4


# ── the surfaces ──────────────────────────────────────────────────────────


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn  # migrated db on disk; the app opens its own connections
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _degraded_run(
    conn: sqlite3.Connection,
    reason: str | None,
    *,
    verdicts: tuple[str | None, ...] = ("keep",),
    started_at: str = "2026-08-03T05:00:00+00:00",
) -> None:
    """A degraded run and the items it left behind.

    `verdicts` is what makes the two stages distinguishable at all: an extraction the
    window stopped leaves items triaged 'keep' with no extraction, and a triage it stopped
    leaves items with no verdict at all — two disjoint populations, only one of which any
    'keep' predicate can count.
    """
    conn.execute(
        "INSERT INTO run (user_id, started_at, finished_at, degraded, degrade_reason)"
        " VALUES (1, ?, ?, 1, ?)",
        (started_at, started_at, reason),
    )
    for n, verdict in enumerate(verdicts):
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, content_hash, triage_verdict) VALUES (1, 'gmail:personal', ?,"
            " '2026-08-03T08:00:00Z', '2026-08-03T08:00:00Z', ?, ?)",
            (f"s{n}", f"h{n}", verdict),
        )
    conn.commit()


class TestNeitherPauseClaimsTheOthersCause:
    def test_the_panel_says_pending_and_retried_not_capped(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _degraded_run(conn, "rate_limit:extract", verdicts=("keep", "keep"))
        panel = panel_slice(client.get("/").text, "panel-sources")
        assert "Model rate limit reached — extraction paused for that run" in panel
        assert "2 items waiting" in panel
        assert "the next scheduled sync retries them" in panel
        # A pause that clears in hours must not hand the owner a month-end date.
        assert "Spend cap" not in panel
        assert "the cap resets" not in panel

    def test_a_triage_stage_limit_counts_unread_items_and_claims_no_triage_pass(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """The sentence the extraction wording got wrong twice over.

        Its count came from a predicate opening with `triage_verdict = 'keep'`, and an
        item triage never reached has no verdict, so the number was structurally zero at
        the moment the most of the ledger was missing. And "extraction paused, triage
        only" is the spend cap's shape borrowed: under the cap triage runs and extraction
        is skipped, while here triage is what stopped and extraction never started.
        """
        _degraded_run(conn, "rate_limit:triage", verdicts=(None, None, None))
        panel = panel_slice(client.get("/").text, "panel-sources")
        assert "Model rate limit reached — the run stopped while reading its new" in panel
        assert "3 items unread" in panel
        assert "0 items" not in panel
        assert "triage only" not in panel
        assert "extraction paused" not in panel
        assert "the cap resets" not in panel

    def test_one_unread_item_is_singular(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _degraded_run(conn, "rate_limit:triage", verdicts=(None,))
        assert "1 item unread" in panel_slice(client.get("/").text, "panel-sources")

    def test_a_staged_row_from_before_the_stage_existed_counts_nothing_it_cannot_know(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """A bare 'rate_limit' cannot say which population is missing, and a count taken
        from the wrong one is worse than no count — that is the whole defect."""
        _degraded_run(conn, "rate_limit", verdicts=(None, "keep"))
        panel = panel_slice(client.get("/").text, "panel-sources")
        assert "Model rate limit reached — the run stopped early" in panel
        assert "items waiting" not in panel and "items unread" not in panel
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
        _degraded_run(conn, "rate_limit:triage", verdicts=(None,))
        page = client.get("/goals").text  # a page with no Sources panel on it
        assert "the next scheduled sync retries them" in page
        assert "the cap resets" not in page

    def test_the_brief_says_it_too(self, conn: sqlite3.Connection, settings: Settings) -> None:
        from backglass.brief import daily

        _degraded_run(conn, "rate_limit:extract")
        brief = daily.build(conn, settings, __import__("datetime").date(2026, 8, 3))
        lines = [line.text for line in brief.all_lines()]
        assert any("Model rate limit reached" in text for text in lines)
        assert not any("Spend cap" in text for text in lines)

    def test_the_costs_report_names_the_rate_limit_not_the_cap(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`backglass costs` was the last reader of the bare boolean, and it told every
        degraded run as the cap's story — a month-long pause with a first-of-the-month
        release, for a run that met a window clearing in hours."""
        from typer.testing import CliRunner

        from backglass import __main__ as cli_mod

        # In the current month, because `backglass costs` reports month-to-date.
        _degraded_run(conn, "rate_limit:extract", started_at=now_iso())
        monkeypatch.setattr(cli_mod, "get_settings", lambda: settings)

        result = CliRunner().invoke(cli_mod.app, ["costs"])

        assert result.exit_code == 0, result.output
        assert "1 run(s) hit a model rate limit this month" in result.output
        assert "spend cap was reached" not in result.output

    def test_the_costs_report_still_names_the_cap_when_it_was_the_cap(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typer.testing import CliRunner

        from backglass import __main__ as cli_mod

        _degraded_run(conn, "spend_cap", started_at=now_iso())
        billed = settings.model_copy(update={"model_backend": "deepinfra"})
        monkeypatch.setattr(cli_mod, "get_settings", lambda: billed)

        result = CliRunner().invoke(cli_mod.app, ["costs"])

        assert result.exit_code == 0, result.output
        assert "spend cap was reached this month" in result.output
        assert "rate limit" not in result.output

    def test_the_cli_prints_the_pending_sentence(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from backglass.__main__ import _print_report
        from backglass.sync import SyncReport

        _print_report(
            SyncReport(degraded=True, degrade_reason="rate_limit:extract"), dry_run=False
        )
        err = capsys.readouterr().err
        assert "model rate limit reached" in err
        assert "the next sync retries them" in err
        assert "spend cap" not in err


# ── the diagnostic must survive the schema it diagnoses ───────────────────


def test_status_reads_a_pre_0016_run_without_dying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status` is the one reader that deliberately does not migrate.

    Every other reader of `degrade_reason` sits behind a `migrate(conn)`; `status` does
    not, because a diagnostic must not mutate the store it is diagnosing. That makes it
    the one command that can meet a pre-0016 `run` row — restore a backup taken before
    this migration, or simply run it first after checkout — and it is precisely the
    command reached for when a run has degraded. Reading the new column unguarded killed
    it with KeyError at exactly that moment.
    """
    from typer.testing import CliRunner

    from backglass import __main__ as cli
    from backglass.config import Settings
    from backglass.db import connect, migrate

    db = tmp_path / "pre0016.db"
    conn = connect(db)
    migrate(conn)
    # The shape of a store written before 0016 shipped: the column simply is not there.
    conn.execute("ALTER TABLE run DROP COLUMN degrade_reason")
    conn.execute(
        "INSERT INTO run (user_id, started_at, items_fetched, items_extracted,"
        " writes, spend_cents, degraded) VALUES (1, '2026-08-03T20:16:16+00:00',"
        " 19, 0, 0, 2005, 1)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(cli, "get_settings", lambda: Settings(db_path=db))
    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    # NULL and absent both mean the same thing: before 0016, degraded could only be the
    # cap, so the label is the cap's — the same sentence a migrated NULL row renders.
    assert "DEGRADED (spend_cap)" in result.output
