"""One brain over many mouths: what the app detects BETWEEN sources.

Every test here drives the REAL pipeline — sync(), the extraction appliers, the
detectors, the assembler — over a ledger fed by more than one source, with FakeModel
canned responses and zero live calls (the independence half of the claim: nothing
below needs a network, an API key, or the owner's machine state).

The cross-source claims under test:

  - a promise made in one mailbox is closed by a message in another;
  - the same plan heard through two sources is one row, not two;
  - the assembled context (what every model call carries) reads across sources;
  - two sources colliding on the same hour becomes a question;
  - evidence in chat resets the staleness clock on a commitment from mail;
  - one dead source degrades — the others still ingest AND the detectors still
    run over what arrived;
  - a new item from a second source re-plans the day the first source planned.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from backglass import context, notify, questions
from backglass.config import Settings
from backglass.connectors.boundary import Boundary
from backglass.ledger import USER_ID
from backglass.sync import sync
from tests.conftest import FakeGmailService, FakeModel, gmail_message, make_connector

PHOENIX = ZoneInfo("America/Phoenix")
#: A Monday inside every fixture's horizon.
TODAY = date(2026, 8, 24)


def _mail(spec: dict[str, Any]) -> dict[str, Any]:
    base = {
        "to": "alex.rivera@example.com",
        "date": "Mon, 17 Aug 2026 09:00:00 -0700",
    }
    base.update(spec)
    return gmail_message(base)


def _sync_two_mailboxes(
    conn: sqlite3.Connection,
    settings: Settings,
    boundary: Boundary,
    model: FakeModel,
    work: list[dict[str, Any]],
    personal: list[dict[str, Any]],
) -> Any:
    return sync(
        conn,
        settings,
        [
            make_connector([_mail(m) for m in work], boundary, label="work"),
            make_connector([_mail(m) for m in personal], boundary, label="personal"),
        ],
        model,
    )


def _calendar_event(
    conn: sqlite3.Connection, day: date, start: str, end: str, title: str
) -> int:
    """A confirmed calendar engagement, the shape calendar:apple ingestion leaves —
    a second source speaking about the owner's hours."""
    from backglass.db import now_iso

    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'calendar:apple', ?, ?, ?, ?, '', '{}', ?, 'keep')",
        (USER_ID, f"cal-{title}-{day}", now_iso(), f"{day}T{start}:00-07:00", title,
         f"h-cal-{title}-{day}"),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
        " when_is_explicit, status, confidence, source_item_id, created_at)"
        " VALUES (?, 'professional', ?, ?, ?, 1, 'confirmed', 0.95, ?, ?)",
        (USER_ID, title, f"{day}T{start}:00-07:00", f"{day}T{end}:00-07:00", sid,
         now_iso()),
    )
    return sid


def _all_days(settings: Settings) -> Settings:
    return settings.model_copy(update={
        "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    })


class TestAcrossMailboxes:
    """gmail:work and gmail:personal are distinct `source` values in the ledger; the
    detections must not care which mouth said what."""

    PROMISE = {
        "id": "w1",
        "from": "Dana Whitfield <dwhitfield@example.gov>",
        "subject": "Transcript request",
        "date": "Mon, 10 Aug 2026 09:00:00 -0700",
        "body": "Could you send me your transcript by the 21st?",
    }
    DELIVERY = {
        "id": "p1",
        "from": "Dana Whitfield <dwhitfield@example.gov>",
        "subject": "Re: got it",
        "date": "Mon, 17 Aug 2026 10:00:00 -0700",
        "body": "Got the transcript, thank you — all set.",
    }
    RESPONSES = {
        "Transcript request": {
            "commitments": [{
                "direction": "i_owe",
                "counterparty": "Dana Whitfield <dwhitfield@example.gov>",
                "what": "send transcript to Dana",
                "due_at": "2026-08-21",
                "due_is_explicit": True,
                "estimated_minutes": None,
                "confidence": 0.9,
                "evidence": "Could you send me your transcript by the 21st?",
                "resolves": False,
                "resolves_what": None,
            }]
        },
        "Re: got it": {
            "commitments": [{
                "direction": "i_owe",
                "counterparty": "Dana Whitfield <dwhitfield@example.gov>",
                "what": "send transcript to Dana",
                "due_at": None,
                "due_is_explicit": False,
                "estimated_minutes": None,
                "confidence": 0.9,
                "evidence": "Got the transcript, thank you — all set.",
                "resolves": True,
                "resolves_what": "send transcript to Dana",
            }]
        },
    }

    def test_a_promise_in_one_mailbox_is_closed_from_the_other(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        report = _sync_two_mailboxes(
            conn, settings, boundary, FakeModel(self.RESPONSES),
            work=[self.PROMISE], personal=[self.DELIVERY],
        )
        assert report.commitments_superseded == 1

        rows = conn.execute(
            "SELECT c.status, si.source FROM commitment c"
            " JOIN source_item si ON si.id = c.source_item_id ORDER BY c.id"
        ).fetchall()
        by_source = {str(r["source"]): str(r["status"]) for r in rows}
        # The promise arrived through work and is closed; the closer came through
        # personal — the ledger saw one life, not two mailboxes.
        assert by_source["gmail:work"] == "superseded"

        # And both sightings hang off ONE Dana, not one entity per mailbox — entity
        # resolution is itself a cross-source join.
        people = conn.execute(
            "SELECT COUNT(*) AS n FROM entity WHERE canonical_name LIKE '%Whitfield%'"
        ).fetchone()
        assert people["n"] == 1

    def test_the_same_plan_heard_twice_is_one_row(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """Work mail confirms dinner; personal mail restates it. One engagement."""
        plan = {
            "kind": "social",
            "what": "dinner at Ravi's",
            "people": ["Priya Raman <priya@example.com>"],
            "starts_at": "2026-08-21T19:00",
            "ends_at": None,
            "when_is_explicit": True,
            "location": "Ravi's on 5th",
            "status": "confirmed",
            "replaces_earlier": False,
            "replaces_start_at": None,
            "confidence": 0.9,
            "evidence": "Friday 7pm at Ravi's, Priya's coming.",
        }
        responses = {
            "Dinner plan": {"commitments": [], "engagements": [plan]},
            "Re: dinner": {"commitments": [], "engagements": [dict(plan)]},
        }
        _sync_two_mailboxes(
            conn, settings, boundary, FakeModel(responses),
            work=[{
                "id": "w2", "from": "Priya Raman <priya@example.com>",
                "subject": "Dinner plan",
                "date": "Mon, 17 Aug 2026 09:00:00 -0700",
                "body": "Friday 7pm at Ravi's, Priya's coming.",
            }],
            personal=[{
                "id": "p2", "from": "Priya Raman <priya@example.com>",
                "subject": "Re: dinner",
                "date": "Mon, 17 Aug 2026 11:00:00 -0700",
                "body": "Friday 7pm at Ravi's, Priya's coming.",
            }],
        )
        n = conn.execute("SELECT COUNT(*) AS n FROM engagement").fetchone()["n"]
        assert n == 1, "two sources restating one dinner must not double-book the day"


class TestTheAssembledPicture:
    def test_context_reads_across_three_sources_in_one_block(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """The block every model call carries mixes mail, calendar and touchpoint
        evidence — the full picture, not one source's view."""
        _sync_two_mailboxes(
            conn, settings, boundary,
            FakeModel(TestAcrossMailboxes.RESPONSES),
            work=[TestAcrossMailboxes.PROMISE], personal=[],
        )
        _calendar_event(conn, date(2026, 8, 25), "10:00", "11:00", "BIO 181 lecture")
        # A touchpoint the owner recorded by hand — a third mouth.
        sid = _calendar_event(conn, date(2026, 8, 20), "18:00", "19:00", "McKenna dinner")
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name, tags_json)"
            " VALUES (?, 'person', 'Felipe Batalini', '[\"connection\"]')", (USER_ID,),
        )
        pid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO touchpoint (user_id, entity_id, kind, occurred_at,"
            " source_item_id, created_at) VALUES (?, ?, 'met', '2026-08-20', ?,"
            " '2026-08-20T00:00:00Z')",
            (USER_ID, pid, sid),
        )

        block = context.assemble(conn, settings, day=TODAY)

        assert '"send transcript to Dana"' in block          # from gmail:work
        assert '"BIO 181 lecture" on 2026-08-25' in block     # from calendar:apple
        assert "Felipe Batalini" in block                     # from the touchpoint
        assert "Dana Whitfield" in block                      # entity via mail evidence
        sources = {
            str(r["source"]) for r in conn.execute("SELECT DISTINCT source FROM source_item")
        }
        assert {"gmail:work", "calendar:apple"} <= sources


class TestCollisionsBetweenSources:
    def test_mail_and_calendar_colliding_on_one_hour_becomes_a_question(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """The calendar says BIO 181 at 10:00; a mail thread confirms an advising call
        at 10:30. Neither source can see the other — the ledger can."""
        call = {
            "kind": "professional",
            "what": "AAMC advising call",
            "people": [],
            "starts_at": "2026-08-24T10:30",
            "ends_at": "2026-08-24T11:15",
            "when_is_explicit": True,
            "location": None,
            "status": "confirmed",
            "replaces_earlier": False,
            "replaces_start_at": None,
            "confidence": 0.9,
            "evidence": "Confirmed: advising call Monday 10:30.",
        }
        _sync_two_mailboxes(
            conn, settings, boundary,
            FakeModel({"Advising call": {"commitments": [], "engagements": [call]}}),
            work=[{
                "id": "w3", "from": "advising@example.edu",
                "subject": "Advising call",
                "date": "Mon, 17 Aug 2026 09:00:00 -0700",
                "body": "Confirmed: advising call Monday 10:30.",
            }],
            personal=[],
        )
        _calendar_event(conn, TODAY, "10:00", "11:00", "BIO 181")

        found = questions.detect(conn, _all_days(settings), TODAY)
        conflicts = [q for q in found if q.kind == "conflict"]
        assert len(conflicts) == 1
        assert "BIO 181" in conflicts[0].detail
        assert "AAMC advising call" in conflicts[0].detail

    def test_chat_evidence_resets_a_mail_commitments_silence(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """Stale detection judges the NEWEST evidence wherever it came from: a mail
        promise nobody mailed about again is not stale while a chat keeps nudging."""
        from backglass.db import now_iso

        old_promise = dict(TestAcrossMailboxes.PROMISE)
        old_promise["date"] = "Sat, 20 Jun 2026 10:00:00 -0700"
        old_promise["body"] = "Could you send me your transcript by the 21st?"
        responses = {
            "Transcript request": {
                "commitments": [{
                    **TestAcrossMailboxes.RESPONSES["Transcript request"]["commitments"][0],
                    "due_at": "2026-07-01",
                }]
            }
        }
        _sync_two_mailboxes(
            conn, settings, boundary, FakeModel(responses),
            work=[old_promise], personal=[],
        )
        cid = int(conn.execute("SELECT id FROM commitment").fetchone()["id"])

        sett = _all_days(settings)
        assert [q.subject_key for q in questions._stale_commitments(conn, sett, TODAY)] \
            == [str(cid)], "overdue and silent in mail: the question fires"

        # A chat message two days ago mentions it — different source, clock reset.
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, title, body_text, content_hash, triage_verdict)"
            " VALUES (?, 'imessage', 'chat-nudge', ?, '2026-08-22T09:00:00-07:00',"
            " 'Dana', 'any word on the transcript?', 'h-chat-nudge', 'keep')",
            (USER_ID, now_iso()),
        )
        nudge = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO commitment_evidence (user_id, commitment_id, source_item_id,"
            " kind, seen_at) VALUES (?, ?, ?, 'restated', ?)",
            (USER_ID, cid, nudge, now_iso()),
        )
        assert questions._stale_commitments(conn, sett, TODAY) == []


class TestIndependence:
    def test_one_dead_source_does_not_blind_the_rest(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rule 5, end to end: work mail is down; personal still ingests, the
        detectors still run over what arrived, and the banner still fires — a broken
        source costs its own items, never the pipeline's judgment."""
        from backglass.connectors.gmail import GmailConnector

        broken_service = FakeGmailService([])
        broken_service.profile_raises = True
        broken_service.history_raises = True
        broken = GmailConnector(label="work", service=broken_service, boundary=boundary)
        healthy = make_connector(
            [_mail(TestAcrossMailboxes.PROMISE)], boundary, label="personal"
        )

        report = sync(
            conn, settings, [broken, healthy],
            FakeModel(TestAcrossMailboxes.RESPONSES),
        )
        assert "gmail:work" in report.failed_sources
        assert report.exit_code == 1
        assert report.commitments_inserted == 1, "the healthy mailbox still produced"

        # Detection and notification run on the surviving data.
        sett = _all_days(settings)
        monkeypatch.setattr(notify, "_deliver", lambda *_a: "osascript")
        banner_day = datetime(2026, 8, 21, 9, 0, tzinfo=PHOENIX)  # its due date
        sent = notify.run(conn, sett, now=banner_day)
        assert "overdue-today" in [s.kind for s in sent]
        assert "send transcript to Dana" in sent[0].body

    def test_no_test_here_touched_a_network_or_a_real_store(self) -> None:
        """The independence claim, stated as an assertion about this file: every
        model call is FakeModel, every mailbox is FakeGmailService, every calendar
        row is written by the test. Nothing imports a live client, reads the owner's
        db path, or needs credentials — which is why this suite can run on a bare
        clone and still exercise every cross-source detection above."""
        import inspect
        import sys

        src = inspect.getsource(sys.modules[__name__])
        # Assembled at runtime so the banned list does not ban itself out of the file.
        banned = ["google" + "apiclient", "anthro" + "pic", "data/" + "backglass.db",
                  "htt" + "px"]
        for term in banned:
            assert term not in src


class TestReplanAcrossSources:
    def test_a_calendar_item_from_a_second_source_replans_the_mail_built_day(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Morning plan is built from mail commitments; mid-day the calendar source
        delivers a new fixed event. The fingerprint sees the world move and the
        proposed plan follows it — dynamic scheduling driven from a DIFFERENT source
        than the one the plan was built from."""
        from backglass.plan import planner, replan

        monkeypatch.setattr(notify, "_deliver", lambda *_a: "osascript")
        sett = _all_days(settings)
        _sync_two_mailboxes(
            conn, sett, boundary, FakeModel(TestAcrossMailboxes.RESPONSES),
            work=[TestAcrossMailboxes.PROMISE], personal=[],
        )
        at_nine = datetime(2026, 8, 24, 9, 0, tzinfo=PHOENIX)
        planner.persist(conn, sett, planner.propose(conn, sett, TODAY, now=at_nine))
        assert replan.run(conn, sett, now=at_nine) is None, "no drift yet"

        _calendar_event(conn, TODAY, "14:00", "15:30", "Surprise lab meeting")

        out = replan.run(conn, sett, now=at_nine.replace(hour=10))
        assert out is not None and out.action == "replaced"
        live_blocks = conn.execute(
            "SELECT pb.title FROM plan_block pb"
            " JOIN day_plan dp ON dp.id = pb.day_plan_id"
            " WHERE dp.status != 'superseded'"
        ).fetchall()
        assert "Surprise lab meeting" in [str(r["title"]) for r in live_blocks]
        note = conn.execute("SELECT kind FROM notification").fetchone()
        assert note["kind"] == "plan-replaced"
