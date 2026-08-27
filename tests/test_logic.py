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
        assert recorded[0].title.endswith(str(cid))
        assert "reported-done" in str(recorded[0].reasoning)
        # And it is filed as the machine's work, not among the owner's own rulings.
        from backglass import decisions

        assert decisions.active(conn) == []

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


# ── expiry: obligations that time itself answers ───────────────────────────
#
# The owner, 2026-08-20: "it is planning for things that are obviously done, for example i
# already moved in on the 9th". Each rule is tested on the row it must close and on the
# nearest-neighbour row it must not, because the neighbour is where the data loss lives.


class TestEventsWhoseDayHasPassed:
    def test_an_event_attended_before_today_is_closed(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Commitment 8 on the live ledger, verbatim. `staleness` waits fourteen days and
        then only asks; this closes it the morning after."""
        cid = a_commitment(
            conn, "Move-in: Willow Hall 502, 8:00am", due="2026-08-09"
        )

        disposals = logic._events_whose_day_has_passed(conn, TODAY)

        assert [d.subject_id for d in disposals] == [cid]
        assert disposals[0].action == "dropped"
        assert disposals[0].rule == "event-day-passed"

    def test_a_deliverable_past_due_is_left_alone(self, conn) -> None:  # type: ignore[no-untyped-def]
        """The whole safety of the rule. A housing contract is still owed the week after
        it was due; closing it is the silent data loss the module exists to refuse."""
        a_commitment(conn, "Submit the signed housing contract", due="2026-08-09")
        a_commitment(conn, "Email the list of transfer credits", due="2026-07-01")

        assert logic._events_whose_day_has_passed(conn, TODAY) == []

    def test_todays_event_survives_until_tomorrow(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Strictly `<` today. The owner moves between UTC-7 and UTC+5:30, so a same-day
        comparison would retire tonight's obligations from the other side of the world."""
        a_commitment(conn, "Attend the BIO 181 review session", due=TODAY.isoformat())

        assert logic._events_whose_day_has_passed(conn, TODAY) == []

    def test_a_future_event_survives(self, conn) -> None:  # type: ignore[no-untyped-def]
        a_commitment(conn, "Attend orientation", due="2026-09-01")
        assert logic._events_whose_day_has_passed(conn, TODAY) == []

    def test_an_undated_attendance_row_is_left_alone(self, conn) -> None:  # type: ignore[no-untyped-def]
        """No date, no contradiction. The rule closes on a day that ended, and a row
        without one has not had a day end."""
        a_commitment(conn, "Attend the alumni mixer", due=None)
        assert logic._events_whose_day_has_passed(conn, TODAY) == []

    def test_an_already_closed_row_is_not_disposed_of_twice(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Rule 3: a second pass over an unchanged ledger writes nothing."""
        a_commitment(conn, "Attend move-in", due="2026-08-09", status="dropped")
        assert logic._events_whose_day_has_passed(conn, TODAY) == []


class TestCanvasPastGrace:
    def _assignment(self, conn: sqlite3.Connection, what: str, due: str) -> int:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, content_hash, triage_verdict)"
            " VALUES (?, 'canvas:ics', ?, ?, ?, 'BIO 181', ?, 'body', ?, 'keep')",
            (USER_ID, f"assignment:{what}", now_iso(), due, what, f"h-{what}"),
        )
        source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes,"
            " estimate_source, confidence, status, source_item_id, created_at)"
            " VALUES (?, 'i_owe', ?, ?, 30, 'manual', 0.9, 'open', ?, ?)",
            (USER_ID, what, due, source_id, now_iso()),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def test_an_assignment_past_the_grace_window_is_closed(self, conn) -> None:  # type: ignore[no-untyped-def]
        cid = self._assignment(conn, "Problem set 3", "2026-08-01")

        disposals = logic._canvas_assignments_past_grace(conn, TODAY)

        assert [d.subject_id for d in disposals] == [cid]
        assert disposals[0].rule == "canvas-past-grace"

    def test_an_assignment_inside_the_grace_window_is_left_alone(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Seven days is the late-submission window most courses allow. Closing on day
        one would retire work the owner is still entitled to hand in."""
        self._assignment(conn, "Lab writeup", "2026-08-14")
        assert logic._canvas_assignments_past_grace(conn, TODAY) == []

    def test_the_boundary_day_is_inclusive_of_the_grace(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Exactly `CANVAS_GRACE_DAYS` old still survives; one day older does not."""
        edge = TODAY.fromordinal(TODAY.toordinal() - logic.CANVAS_GRACE_DAYS)
        self._assignment(conn, "Edge", edge.isoformat())

        assert logic._canvas_assignments_past_grace(conn, TODAY) == []

    def test_a_past_due_obligation_from_mail_is_not_touched(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Bounded by source, never by shape. A professor's mail asking for the same
        essay is still owed — only the feed row with no submission state expires."""
        a_commitment(conn, "Submit the BIO 181 essay", due="2026-08-01")
        assert logic._canvas_assignments_past_grace(conn, TODAY) == []


# ══ 2026-08-24: three more contradictions the ledger was already holding ═══


def a_reminder(conn: sqlite3.Connection, what: str, *, completed_at: str | None = None) -> int:
    """A Reminders-sourced commitment, optionally with its completion item beside it.

    The completion is a *second* `source_item` sharing the reminder's UUID with a
    `:completed:` suffix — the shape Apple Reminders actually emits and the connector
    actually stores, which is what makes the rule a join rather than a guess.
    """
    uuid = f"x-apple-reminder://{what.replace(' ', '-')}"
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'reminders', ?, ?, '2026-01-06T09:00:00-07:00', 'me', ?, 'b', ?, 'keep')",
        (USER_ID, uuid, now_iso(), what, f"h-{uuid}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes,"
        " estimate_source, confidence, status, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, '2026-01-06', 30, 'manual', 0.9, 'open', ?, ?)",
        (USER_ID, what, source_id, now_iso()),
    )
    commitment_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    if completed_at:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, content_hash, triage_verdict)"
            " VALUES (?, ?, ?, ?, ?, 'me', ?, 'b', ?, 'drop')",
            (
                USER_ID,
                "reminders",
                f"{uuid}:completed:{completed_at}",
                now_iso(),
                completed_at,
                what,
                f"h-{uuid}-done",
            ),
        )
    return commitment_id


class TestACompletedReminderClosesItsCommitment:
    """Found by asking why "Clean fishtank", due in January, led the owner's week.

    Reminders emits a second item when an entry is ticked; triage drops it (correctly —
    it carries no obligation) and nothing else ever looked at it. Six had been ingested
    and five open commitments were standing behind them.
    """

    def test_the_ticked_reminder_resolves_it(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        cid = a_reminder(conn, "Clean fishtank", completed_at="2026-08-12T18:54:31Z")

        logic.run(conn, sett, TODAY)

        row = conn.execute("SELECT status, resolution_note FROM commitment WHERE id = ?",
                           (cid,)).fetchone()
        assert row["status"] == "done", "resolved, not dropped — the owner did the thing"
        assert "marked complete on 2026-08-12" in str(row["resolution_note"])

    def test_a_reminder_nobody_ticked_is_left_alone(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The near-miss, and the whole boundary: an untouched reminder is *silence*,
        which is `staleness`'s business and asks rather than closes."""
        cid = a_reminder(conn, "Water the plants")

        logic.run(conn, sett, TODAY)

        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"] == "open"

    def test_a_completion_of_a_different_reminder_does_not_reach_it(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The join is on the UUID, so two chores with similar words never touch."""
        open_one = a_reminder(conn, "Clean the garage")
        a_reminder(conn, "Clean fishtank", completed_at="2026-08-12T18:54:31Z")

        logic.run(conn, sett, TODAY)

        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (open_one,)
        ).fetchone()["status"] == "open"


class TestRetractedEvidenceTakesItsCommitment:
    def test_a_retracted_source_item_drops_the_commitment(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """`retraction.py` certifies this only from a windowed complete read, so it is a
        positive statement that the evidence is gone — never an inference from silence."""
        cid = a_commitment(conn, "submit the withdrawn form", due="2026-09-01")
        item = conn.execute(
            "SELECT source_item_id AS s FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["s"]
        conn.execute(
            "INSERT INTO source_item_retraction (source_item_id, user_id, retracted_at,"
            " reason) VALUES (?, ?, '2026-08-20T00:00:00Z', 'gone from a complete read')",
            (item, USER_ID),
        )

        logic.run(conn, sett, TODAY)

        row = conn.execute("SELECT status, resolution_note FROM commitment WHERE id = ?",
                           (cid,)).fetchone()
        assert row["status"] == "dropped", "nothing says the work happened"
        assert "retracted on 2026-08-20" in str(row["resolution_note"])

    def test_a_commitment_whose_evidence_still_exists_is_untouched(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        cid = a_commitment(conn, "submit the live form", due="2026-09-01")

        logic.run(conn, sett, TODAY)

        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"] == "open"


class TestAnUpstreamDueDateThatMoved:
    """Goal 4 increment B1. Five CIS 236 assignments moved and the ledger never heard,
    because a re-read of an immutable `source_item` is a conflict that gets logged and
    dropped. The `assignment` mirror is the half that is allowed to change."""

    def _with_assignment(
        self, conn: sqlite3.Connection, *, ledger_due: str, feed_due: str
    ) -> int:
        cid = a_commitment(conn, "Submit the Team Charter", due=ledger_due)
        item = conn.execute(
            "SELECT source_item_id AS s FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["s"]
        conn.execute(
            "INSERT INTO assignment (user_id, source, external_id, source_item_id, course,"
            " title, due_at, description_hash, first_seen_at, last_changed_at)"
            " VALUES (?, 'canvas:ics', 'assignment:1', ?, 'CIS 236', 'Team Charter', ?,"
            " 'h-desc', '2026-08-01T00:00:00Z', '2026-08-22T00:00:00Z')",
            (USER_ID, item, feed_due),
        )
        return cid

    def test_the_commitment_moves_to_the_feeds_date(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        cid = self._with_assignment(conn, ledger_due="2026-08-31", feed_due="2026-09-04")

        report = logic.run(conn, sett, TODAY)

        row = conn.execute("SELECT status, due_at, resolution_note FROM commitment"
                           " WHERE id = ?", (cid,)).fetchone()
        assert str(row["due_at"])[:10] == "2026-09-04"
        assert row["status"] == "open", "a move is not a close"
        assert "was due 2026-08-31" in str(row["resolution_note"])
        assert [d.action for d in report.disposals if d.subject_id == cid] == ["moved"]

    def test_the_move_is_recorded_as_a_change_and_knocked(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """A deadline that moves under a plan must not be discovered by missing it.

        `notify_window` is widened for the test rather than left at its default, because
        `notify.record` reads the *real* wall clock: the default 08:00-22:00 made this
        test pass all day and fail at 23:15, which is a test that reports the hour it was
        run at rather than whether the code works.
        """
        cid = self._with_assignment(conn, ledger_due="2026-08-31", feed_due="2026-09-04")

        logic.run(conn, sett.model_copy(update={"notify_window": "00:00-23:59"}), TODAY)

        event = conn.execute(
            "SELECT field, old_value, new_value, cause FROM claim_event"
            " WHERE subject_table = 'commitment' AND subject_id = ?", (cid,)
        ).fetchone()
        assert event["field"] == "due_at"
        assert (event["old_value"], event["new_value"]) == ("2026-08-31", "2026-09-04")
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM notification WHERE kind = 'deadline-moved'"
        ).fetchone()["n"] == 1

    def test_an_agreeing_feed_moves_nothing(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Rule 3 for this rule: the ordinary case is that the two agree, and it must
        write nothing at all."""
        cid = self._with_assignment(conn, ledger_due="2026-08-31", feed_due="2026-08-31")

        logic.run(conn, sett, TODAY)

        assert conn.execute(
            "SELECT COUNT(*) AS n FROM claim_event WHERE subject_id = ?", (cid,)
        ).fetchone()["n"] == 0

    def test_a_second_pass_over_a_moved_row_writes_nothing(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Idempotency, and this rule is the one where it could plausibly fail: it does
        not close the row it acts on, so nothing about its own disposal stops it running
        again. What stops it is that the row now agrees with the feed."""
        self._with_assignment(conn, ledger_due="2026-08-31", feed_due="2026-09-04")
        logic.run(conn, sett, TODAY)

        before = conn.execute("SELECT COUNT(*) AS n FROM claim_event").fetchone()["n"]
        second = logic.run(conn, sett, TODAY)

        assert not [d for d in second.disposals if d.rule == "upstream-due-moved"]
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM claim_event"
        ).fetchone()["n"] == before


class TestADuplicateCardWhosePairCannotMerge:
    """Found the day the `duplicate_commitment` kind shipped: the relevance judge dropped
    two rows that duplicate cards were already asking about, and the cards stayed on the
    board. Answering one would call `actions.same_thing` on a closed row and do nothing.
    """

    def _card(self, conn: sqlite3.Connection, a_id: int, b_id: int) -> int:
        conn.execute(
            "INSERT INTO open_question (user_id, kind, subject_key, question, detail,"
            " options_json, asked_at) VALUES (?, 'duplicate_commitment', ?, ?, '', '[]', ?)",
            (USER_ID, f"{a_id}|{b_id}", "Are these the same promise?", now_iso()),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def test_one_side_closing_is_enough_to_moot_it(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Either side, not both: a merge needs two open rows, so one closure is already
        the end of the question."""
        alive = a_commitment(conn, "self-select first-year room")
        gone = a_commitment(conn, "Self-select first-year housing room", status="dropped")
        qid = self._card(conn, alive, gone)

        logic.run(conn, sett, TODAY)

        row = conn.execute(
            "SELECT status, answer_text FROM open_question WHERE id = ?", (qid,)
        ).fetchone()
        assert row["status"] == "moot"
        assert "a merge needs two open rows" in str(row["answer_text"])

    def test_a_card_whose_pair_is_both_open_is_left_alone(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The near-miss. This is a live question the owner still has to answer."""
        first = a_commitment(conn, "Upload ASU ID photo and verify identity")
        second = a_commitment(conn, "upload ASU ID photo and verify identity")
        qid = self._card(conn, first, second)

        logic.run(conn, sett, TODAY)

        assert conn.execute(
            "SELECT status FROM open_question WHERE id = ?", (qid,)
        ).fetchone()["status"] == "open"
