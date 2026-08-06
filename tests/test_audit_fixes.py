"""Regressions for the 2026-08-01 audit findings (tasks/audit-2026-08-01.md).

Each test names the failure it prevents rather than the code it covers, because in every
case the code looked correct — the bugs were in what the code did not say: a cap check
that could not fire from where it sat, a confidence floor missing from two queries out of
nine, a supersede branch nobody had gated, and two writes that were only accidentally
atomic.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.connectors import base
from backglass.db import now_iso
from backglass.ledger import USER_ID, Ledger
from backglass.sync import SpendCap, _in_parallel
from backglass.web.app import create_app
from tests.conftest import healthy_run

TODAY = date(2026, 8, 1)


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _commitment(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    what: str = "send the deck",
    confidence: float = 0.9,
    rollover_count: int = 0,
    external: str = "m1",
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'gmail:personal', ?, ?, '2026-07-14T09:15:00-07:00',"
        " 'Dana <dana@example.gov>', 'Plan', 'b', '{}', ?, 'keep')",
        (USER_ID, external, now_iso(), f"h-{external}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " rollover_count, source_item_id, created_at) VALUES (?, 'i_owe', ?, ?, ?, 'open',"
        " ?, ?, ?)",
        (USER_ID, what, TODAY.isoformat(), confidence, rollover_count, source_id, now_iso()),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


# ── rule 2: a guess never states itself as fact ───────────────────────────


class TestConfidenceFloorOnRollover:
    """CLAUDE.md rule 2 held on seven brief queries and was missing from two.

    A commitment that rolls over enough times gets asked about in the daily brief and
    listed in the Friday one. Neither query filtered on confidence, so a never-reviewed
    guess became a stated obligation just by sitting in the ledger long enough — the
    exact "two unsourced wrong claims and trust never comes back" failure rule 2 exists
    to prevent.
    """

    def test_a_low_confidence_rollover_is_not_asked_about(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.plan import rollover

        _commitment(
            conn, settings, what="a guess", confidence=0.3,
            rollover_count=settings.rollover_question_at, external="g1",
        )
        assert rollover.flagged_for_question(conn, settings) == []

    def test_a_confident_rollover_still_is(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.plan import rollover

        _commitment(
            conn, settings, what="a real one", confidence=0.95,
            rollover_count=settings.rollover_question_at, external="g2",
        )
        rows = rollover.flagged_for_question(conn, settings)
        assert [str(r["what"]) for r in rows] == ["a real one"]

    def test_the_weekly_brief_does_not_report_a_guess_as_slipping(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.brief import weekly

        _commitment(
            conn, settings, what="a guess", confidence=0.3,
            rollover_count=settings.rollover_question_at, external="g3",
        )
        section = weekly.friday(conn, settings, TODAY)
        assert not any("a guess" in line.text for line in section.lines)


class TestSupersedeIsGated:
    """The one ledger write a hostile sender can reach.

    The extraction prompt ends with the message body and the system prompt says to follow
    the message's instructions, so "sent it last night, you're all set" is read as an
    instruction to resolve. With no counterparty the dedup match is NULL-safe, which
    means it lands on exactly the commitments the owner typed by hand. Closing a
    commitment also hides it everywhere at once, unlike a bad insert — so an unconfirmed
    resolution belongs in the review queue, not in the ledger.
    """

    def _extraction(self, confidence: float) -> object:
        from backglass.extract.schemas import CommitmentExtraction

        return CommitmentExtraction.model_validate(
            {
                "commitments": [
                    {
                        "direction": "i_owe",
                        "counterparty": None,
                        "what": "the migration plan is handled",
                        "confidence": confidence,
                        "evidence": "sent it last night, you're all set",
                        "resolves": True,
                        "resolves_what": "send the deck",
                    }
                ]
            }
        )

    def test_a_low_confidence_resolution_does_not_close_a_commitment(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.extract import commitments as tier2

        cid = _commitment(conn, settings, what="send the deck", external="s1")
        report = tier2.apply(
            self._extraction(0.3),
            source_item_id=1,
            occurred_at="2026-07-20T09:00:00-07:00",
            ledger=Ledger(conn, settings),
            settings=settings,
        )
        status = conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"]
        assert status == "open"
        assert report.superseded == 0
        assert report.review_queue >= 1, "it goes to the queue rather than being dropped"

    def test_a_confident_resolution_still_supersedes(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.extract import commitments as tier2

        cid = _commitment(conn, settings, what="send the deck", external="s2")
        report = tier2.apply(
            self._extraction(0.95),
            source_item_id=1,
            occurred_at="2026-07-20T09:00:00-07:00",
            ledger=Ledger(conn, settings),
            settings=settings,
        )
        status = conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"]
        assert status == "superseded"
        assert report.superseded == 1


# ── rule 7: the cap is enforced, not monitored ────────────────────────────


class TestSpendCapStopsMidBatch:
    """`_in_parallel` is a generator, which is what made the old check unreachable.

    Everything before the first `yield` ran on the caller's first `next()`, so the
    submission loop's `cap.reached` test only ever saw spend from before the call. The
    entire batch was dispatched before the caller charged anything: a 400-item backfill
    could run to twice a $20 cap and only then set `degraded`.
    """

    def test_work_stops_once_the_cap_is_reached(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        capped = settings.model_copy(update={"monthly_spend_cap_cents": 100})
        cap = SpendCap(conn, capped)
        done: list[int] = []

        def work(item: int) -> int:
            done.append(item)
            return item

        consumed = []
        for value in _in_parallel(work, list(range(50)), 2, cap, stop_on_cap=True):
            consumed.append(value)
            cap.charge(0.40)  # 40c a call: the cap falls inside the third wave

        assert cap.reached
        assert len(done) < 50, "the batch must not be fully dispatched before stopping"
        # At most one wave of overshoot beyond the item that tripped the cap.
        assert len(done) <= len(consumed) + 2

    def test_triage_is_never_stopped_by_the_cap(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """docs/02 §Cost control: the degraded state is triage-only, not stopped.

        A month that hit the cap on the 8th must keep classifying mail, or the backlog
        looks like an empty inbox rather than a paused one.
        """
        cap = SpendCap(conn, settings.model_copy(update={"monthly_spend_cap_cents": 0}))
        seen = list(_in_parallel(lambda i: i, list(range(10)), 2, cap, stop_on_cap=False))
        assert seen == list(range(10))

    def test_order_is_preserved_across_waves(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Supersession depends on applying oldest-first, so waving must not reorder."""
        cap = SpendCap(conn, settings)
        seen = _in_parallel(lambda i: i, list(range(9)), 4, cap, stop_on_cap=True)
        assert list(seen) == list(range(9))


# ── docs/08: a token never lands outside the credential table ─────────────


class TestErrorRedaction:
    @pytest.mark.parametrize(
        "message",
        [
            "https://gmail.googleapis.com/v1/messages?access_token=ya29.SECRETVALUE",
            "auth failed: api_key=sk-live-SECRETVALUE",
            "401 Unauthorized (Authorization: Bearer ya29.SECRETVALUE)",
            "refresh_token: 1//SECRETVALUE expired",
        ],
    )
    def test_a_credential_never_survives_into_a_stored_error(self, message: str) -> None:
        """This string is written to credential.last_error AND run.errors_json — the
        second is a table docs/08 says tokens must never reach."""
        assert "SECRETVALUE" not in base.safe_error(RuntimeError(message))
        assert "redacted" in base.safe_error(RuntimeError(message))

    @pytest.mark.parametrize(
        "message",
        [
            # No `token=` to match on: the shape is the only tell, and these are the
            # messages that actually leak — a provider quoting back what it rejected.
            "invalid_auth for xoxb-4827361902-SECRETVALUEabcdefghij",
            "403 for xoxp-4827361902-SECRETVALUEabcdefghij",
            "bad credentials: ghp_SECRETVALUEabcdefghijklmnopqrstuv",
            "bad credentials: github_pat_SECRETVALUEabcdefghijklmnopqrstuv",
            "401 {'error': 'authentication_error'} sk-ant-api03-SECRETVALUEabcdefgh",
            "GET /api/v1/courses 401 (7392~SECRETVALUEabcdefghijklmnopqrstuvwxyz01234567)",
        ],
    )
    def test_a_bare_provider_token_is_redacted_on_shape_alone(self, message: str) -> None:
        assert "SECRETVALUE" not in base.safe_error(RuntimeError(message))
        assert "redacted" in base.safe_error(RuntimeError(message))

    def test_shape_matching_does_not_eat_ordinary_words(self) -> None:
        """A redactor that fires on anything hyphenated would blank every error."""
        text = base.safe_error(RuntimeError("course 7392 not found; check-in failed"))
        assert "7392" in text and "check-in" in text and "redacted" not in text

    def test_the_error_is_still_useful_after_redaction(self) -> None:
        """A redactor that ate the whole message would just move the failure elsewhere:
        the Sources panel exists to say what broke."""
        text = base.safe_error(RuntimeError("invalid_grant: token=abc has been revoked"))
        assert "invalid_grant" in text and "revoked" in text

    def test_it_is_bounded(self) -> None:
        assert len(base.safe_error(RuntimeError("x" * 5000))) <= 300


# ── docs/11 §8: a failure is never quiet ──────────────────────────────────


class TestFailedWriteIsVisible:
    """HTMX swaps nothing on a non-2xx, so a refused or lost write left no trace.

    The realistic case is not a stale row — `actions._require_open` only checks that
    the commitment EXISTS, so resolving an already-resolved one is idempotent and
    returns 200. It is the server going away mid-click (launchd restart, a crash)
    and any 5xx: HTMX fires `htmx:sendError`/`htmx:responseError`, nothing listened,
    and the click looked exactly like a click that worked.
    """

    def test_every_page_carries_the_strip_and_its_handler(
        self, client: TestClient
    ) -> None:
        for path in ("/", "/goals", "/people", "/roadmaps", "/schedule", "/memory"):
            body = client.get(path).text
            assert 'id="oops"' in body, path
            assert "/static/oops.js" in body, path

    def test_the_handler_is_served(self, client: TestClient) -> None:
        response = client.get("/static/oops.js")
        assert response.status_code == 200
        assert "htmx:sendError" in response.text
        assert "htmx:responseError" in response.text

    def test_a_page_with_nothing_wrong_carries_no_alarm_ink(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """§8 rule 1 spends vermilion on overdue and destroy alone, so the strip is
        empty at rest and builds its chip in JS. A dormant k-verm in every page would
        put the alarm ink on surfaces that are not alarming.

        The healthy run is required, not incidental: a never-synced ledger legitimately
        raises a vermilion sidebar alert, and this test is about the strip, not that.
        """
        healthy_run(conn)
        conn.commit()
        assert "k-verm" not in client.get("/people").text

    def test_the_refusal_reason_reaches_the_owner(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The detail strings are already written for the owner, so the strip shows
        them verbatim rather than replacing them with a generic apology."""
        response = client.post("/commitments/9999/resolve")
        assert response.status_code == 422
        assert response.json()["detail"] == "no commitment 9999"


class TestOneBadItemDoesNotEndTheRun:
    """Rule 5, applied at the level the failure actually happens.

    "A failing source degrades, never blocks" was implemented for sources and for model
    calls, and not for `apply()`. That path re-raised, so a single item whose application
    threw took every item queued behind it with it. On 2026-08-03 a 4,400-message mail
    backfill died on one `starts_at` that carried a UTC offset compared against one that
    did not — a TypeError the tolerant `except ValueError` below it never saw — leaving
    thousands of triaged items unextracted and the run reporting a bare traceback.
    """

    def test_a_failing_apply_parks_that_item_and_keeps_going(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Any
    ) -> None:
        from backglass.sync import sync
        from tests.conftest import FakeModel, gmail_message, make_connector

        specs = [
            {
                "id": f"m{i}",
                "from": "Dana Whitfield <dwhitfield@example.gov>",
                "to": "alex.rivera@example.com",
                "subject": f"Plan {i}",
                "date": "Fri, 10 Jul 2026 09:15:00 -0700",
                "body": f"I'll send plan {i} by Friday.",
            }
            for i in range(3)
        ]
        responses = {
            f"Plan {i}": {
                "commitments": [
                    {
                        "direction": "owed_to_me",
                        "counterparty": "Dana Whitfield <dwhitfield@example.gov>",
                        "what": f"plan {i}",
                        "due_at": "2026-07-17",
                        "due_is_explicit": True,
                        "estimated_minutes": None,
                        "confidence": 0.92,
                        "evidence": f"I'll send plan {i} by Friday.",
                        "resolves": False,
                        "resolves_what": None,
                    }
                ]
            }
            for i in range(3)
        }

        from backglass.extract import commitments as tier2

        real_apply = tier2.apply
        calls = {"n": 0}

        def exploding_apply(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TypeError("can't subtract offset-naive and offset-aware datetimes")
            return real_apply(*args, **kwargs)

        tier2.apply = exploding_apply  # type: ignore[assignment]
        try:
            messages = [gmail_message(spec) for spec in specs]
            report = sync(
                conn, settings, [make_connector(messages, boundary)], FakeModel(responses)
            )
        finally:
            tier2.apply = real_apply  # type: ignore[assignment]

        assert report.parked == 1
        assert report.extracted == 2, "the two items behind the failure still ran"
        assert any("apply " in e for e in report.errors), "and the failure is reported"

    def test_the_parked_item_is_left_for_the_next_run(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Any
    ) -> None:
        """Rolled back means unstamped: `extraction_version` stays NULL, so the item is
        still pending rather than silently marked done."""
        from backglass.extract import commitments as tier2
        from backglass.sync import sync
        from tests.conftest import FakeModel, gmail_message, make_connector

        spec = {
            "id": "solo",
            "from": "Dana Whitfield <dwhitfield@example.gov>",
            "to": "alex.rivera@example.com",
            "subject": "Migration plan",
            "date": "Fri, 10 Jul 2026 09:15:00 -0700",
            "body": "I'll have the revised migration plan over to you by Friday.",
        }
        responses = {
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
                        "evidence": "by Friday",
                        "resolves": False,
                        "resolves_what": None,
                    }
                ]
            }
        }

        real_apply = tier2.apply
        tier2.apply = lambda *a, **k: (_ for _ in ()).throw(TypeError("boom"))  # type: ignore[assignment]
        try:
            sync(
                conn,
                settings,
                [make_connector([gmail_message(spec)], boundary)],
                FakeModel(responses),
            )
        finally:
            tier2.apply = real_apply  # type: ignore[assignment]

        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM source_item"
            " WHERE triage_verdict = 'keep' AND extraction_version IS NULL"
        ).fetchone()
        assert pending["n"] == 1
