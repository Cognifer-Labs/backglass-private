"""The logic checker: dispose of what the record contradicts, never of what it is silent about.

The owner's instruction (2026-08-18): "if something obviously doesnt make sense then
dispose of it yourself". The tests here are mostly about the second half of that — the
line between *obviously* and *probably*, because everything on the wrong side of it is
silent data loss, and four entries in tasks/lessons.md are about paying for that.

So each rule is tested twice: once on the row it must dispose of, once on the
nearest-neighbour row it must leave alone.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from backglass import logic, questions
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID

PHOENIX = "America/Phoenix"
TODAY = date(2026, 8, 18)


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={"default_tz": PHOENIX, "confidence_threshold": 0.7})


def a_commitment(
    conn: sqlite3.Connection,
    what: str,
    *,
    due: str | None = None,
    status: str = "open",
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', ?, ?, '2026-08-01T09:00:00-07:00', 'a@b.com', 'Subj',"
        " 'body', ?, 'keep')",
        (USER_ID, f"m-{what}", now_iso(), f"h-{what}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes,"
        " estimate_source, confidence, status, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, ?, 30, 'manual', 0.9, ?, ?, ?)",
        (USER_ID, what, due, status, source_id, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def a_class(conn: sqlite3.Connection, day: date, start: str, end: str, title: str) -> None:
    """A fixed calendar event, through the path `capacity.day_events` actually reads."""
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'calendar:apple', ?, ?, ?, NULL, ?, '', '{}', ?, 'keep')",
        (USER_ID, f"{title}-{day}-{start}", now_iso(), f"{day}T{start}:00-07:00", title,
         f"h-{title}-{day}-{start}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at, when_is_explicit,"
        " location, status, confidence, source_item_id, created_at)"
        " VALUES (?, 'professional', ?, ?, ?, 1, NULL, 'confirmed', 0.95, ?, ?)",
        (USER_ID, title, f"{day}T{start}:00-07:00", f"{day}T{end}:00-07:00", source_id,
         now_iso()),
    )


def a_question(
    conn: sqlite3.Connection, kind: str, subject_key: str, *, status: str = "open"
) -> int:
    conn.execute(
        "INSERT INTO open_question (user_id, kind, subject_key, question, detail,"
        " options_json, status, asked_at) VALUES (?, ?, ?, 'Well?', '', '[]', ?, ?)",
        (USER_ID, kind, subject_key, status, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def statuses(conn: sqlite3.Connection, table: str) -> dict[int, str]:
    return {
        int(r["id"]): str(r["status"]) for r in conn.execute(f"SELECT id, status FROM {table}")
    }


class TestAnObligationThatReportsItselfDone:
    def test_a_past_tense_report_is_not_something_you_owe(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Live rows 71/148/327: "sent EIN screenshot", "sent attached file/resume",
        "sent updated resume". The extractor read a sentence about the past as a promise
        about the future, and the board then owed the owner something already done."""
        cid = a_commitment(conn, "sent updated resume")

        logic.run(conn, sett, TODAY)

        row = conn.execute(
            "SELECT status, resolution_note FROM commitment WHERE id = ?", (cid,)
        ).fetchone()
        assert row["status"] == "done"
        assert str(row["resolution_note"]).startswith("logic: reported-done")

    def test_trailing_completed_counts(self, conn: sqlite3.Connection, sett: Settings) -> None:
        """Live row 267: "MCAT prep completed", still open and still being planned."""
        cid = a_commitment(conn, "MCAT prep completed", due="2026-03-23")

        logic.run(conn, sett, TODAY)

        assert statuses(conn, "commitment")[cid] == "done"

    def test_an_obligation_that_merely_ends_in_done_is_left_alone(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Live row 48: "zip it up once done" is a real future obligation whose sentence
        happens to contain the word. A trailing-"done" rule would close it, which is why
        there isn't one."""
        cid = a_commitment(conn, "zip it up once done")

        logic.run(conn, sett, TODAY)

        assert statuses(conn, "commitment")[cid] == "open"

    def test_a_future_send_is_left_alone(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """"send transcript" and "resend resume attachment" are the ordinary case: the
        rule is anchored to report verbs at the start, not to the topic."""
        first = a_commitment(conn, "send transcript and AP scores")
        second = a_commitment(conn, "resend resume attachment")

        logic.run(conn, sett, TODAY)

        assert statuses(conn, "commitment") == {first: "open", second: "open"}


class TestQuestionsThatAnswerThemselves:
    def test_a_stale_question_about_a_closed_commitment_is_mooted(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        cid = a_commitment(conn, "accept the award", status="dropped")
        qid = a_question(conn, "stale", str(cid))

        logic.run(conn, sett, TODAY)

        assert statuses(conn, "open_question")[qid] == "moot"

    def test_a_stale_question_about_a_live_commitment_survives(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        cid = a_commitment(conn, "accept the award")
        qid = a_question(conn, "stale", str(cid))

        logic.run(conn, sett, TODAY)

        assert statuses(conn, "open_question")[qid] == "open"

    def test_a_priority_question_about_a_day_that_ended_is_mooted(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """"This did not fit today. Should it have come first?" cannot be acted on once
        the day is over — the plan it asks about is already history."""
        cid = a_commitment(conn, "submit the housing form")
        yesterday = a_question(conn, "priority", f"2026-08-17|{cid}")
        todays = a_question(conn, "priority", f"2026-08-18|{cid}")

        logic.run(conn, sett, TODAY)

        assert statuses(conn, "open_question") == {yesterday: "moot", todays: "open"}

    def test_a_protected_question_is_judged_by_its_commitment_not_by_the_day(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """`protected|{cid}|{routine}` is deliberately ask-once across weeks (a weekly gym
        slot colliding with the same problem set is one question). It carries no day, so
        it is retired only when its commitment closes."""
        live = a_commitment(conn, "finish the problem set")
        closed = a_commitment(conn, "old problem set", status="done")
        kept = a_question(conn, "priority", f"protected|{live}|gym")
        gone = a_question(conn, "priority", f"protected|{closed}|gym")

        logic.run(conn, sett, TODAY)

        assert statuses(conn, "open_question") == {kept: "open", gone: "moot"}

    def test_a_collision_that_left_the_calendar_is_mooted(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The conflict detector is a pure function of the next three weeks of events, so
        absence really is proof: the section was dropped, or the day passed."""
        qid = a_question(conn, "conflict", "BIO 181|LIA 101")

        logic.run(conn, sett, TODAY)

        assert statuses(conn, "open_question")[qid] == "moot"

    def test_a_collision_still_on_the_calendar_survives(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        for title, start, end in (
            ("BIO 181", "10:30", "11:45"),
            ("LIA 101", "10:10", "11:00"),
        ):
            a_class(conn, date(2026, 8, 24), start, end, title)
        pair = "|".join(sorted(("BIO 181", "LIA 101")))
        qid = a_question(conn, "conflict", pair)

        logic.run(conn, sett, TODAY)

        assert statuses(conn, "open_question")[qid] == "open"


class TestWhatDisposalMeans:
    def test_every_disposal_is_recorded_as_a_decision(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """An automatic disposal nobody can find is worse than a wrong one they can."""
        cid = a_commitment(conn, "sent the deposit")

        logic.run(conn, sett, TODAY)

        recorded = logic.recent(conn)
        assert len(recorded) == 1
        assert str(recorded[0]["title"]).endswith(str(cid))
        assert "reported-done" in str(recorded[0]["reasoning"])

    def test_a_dry_run_writes_nothing(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        cid = a_commitment(conn, "sent the deposit")
        qid = a_question(conn, "conflict", "gone|missing")

        report = logic.run(conn, sett, TODAY, dry_run=True)

        assert report.applied == 2
        assert statuses(conn, "commitment")[cid] == "open"
        assert statuses(conn, "open_question")[qid] == "open"
        assert logic.recent(conn) == []

    def test_a_second_pass_over_an_unchanged_ledger_writes_nothing(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Rule 3. Every rule reads a state its own disposal ends, so the pass converges
        after one run — asserted rather than assumed, because a checker that re-decides
        the same rows every sync writes a decision row per sync forever."""
        a_commitment(conn, "sent the deposit")
        a_question(conn, "conflict", "gone|missing")
        logic.run(conn, sett, TODAY)
        before = conn.execute("SELECT COUNT(*) AS n FROM decision").fetchone()["n"]

        second = logic.run(conn, sett, TODAY)

        assert second.applied == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM decision").fetchone()["n"] == before

    def test_an_owner_dismissal_is_never_revived_but_a_mooting_is(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The asymmetry the whole `moot` status exists for. Two classes that stop
        colliding in August and collide again in January are a live question the second
        time; something the owner waved away is not."""
        waved = a_question(conn, "untitled", "New Event|09:00|60", status="dismissed")
        mooted = a_question(conn, "untitled", "Busy|14:00|30", status="moot")

        # A refresh that re-detects both: the detector cannot tell them apart, the
        # statuses can.
        found = [
            questions.Question(
                kind="untitled", subject_key="New Event|09:00|60",
                question="What is it?", detail="", options=["Real", "Junk"],
            ),
            questions.Question(
                kind="untitled", subject_key="Busy|14:00|30",
                question="What is it?", detail="", options=["Real", "Junk"],
            ),
        ]
        import unittest.mock as mock

        with mock.patch.object(questions, "detect", return_value=found):
            questions.refresh(conn, sett, TODAY)

        assert statuses(conn, "open_question") == {waved: "dismissed", mooted: "open"}

    def test_a_row_closed_between_check_and_apply_is_reported_not_raised(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The 2026-08-12 shape: re-read at write time, and a race is a line in the
        report rather than a traceback that takes the sync's exit code with it."""
        from backglass.web import actions

        cid = a_commitment(conn, "sent the deposit")
        report = logic.check(conn, sett, TODAY)
        actions.resolve(conn, cid, note="board click")

        applied = logic.apply(conn, sett, report)

        assert applied.applied == 0
        assert applied.errors and str(cid) in applied.errors[0]
        assert conn.execute(
            "SELECT resolution_note FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["resolution_note"] == "board click"


class TestTheCommandDrivesTheRealDoor:
    def test_the_cli_reports_what_it_disposed_of(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from backglass import __main__ as cli

        a_commitment(conn, "sent the deposit")
        conn.commit()
        monkeypatch.setattr(cli, "get_settings", lambda: sett)

        result = CliRunner().invoke(cli.app, ["logic", "--json"])

        assert result.exit_code == 0, result.output
        assert '"reported-done": 1' in result.output
