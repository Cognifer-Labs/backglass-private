"""Confusions Backglass cannot settle, asked instead of guessed.

The review queue asks about one record's confidence. These are the questions that do not
fit that shape: two classes at the same hour are each perfectly confident and cannot both
be attended; an untitled hour is not a doubtful commitment; two entities may be one
person. Each silently degrades the plan while nothing anywhere says so.

The invariants under test are the ones that decide whether a question surface gets used
twice: it asks once, it never guesses in the meantime, and the owner's own words are a
complete answer rather than a fallback.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backglass import decisions, questions
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan.capacity import FixedEvent

MONDAY = date(2026, 8, 24)
PHOENIX = ZoneInfo("America/Phoenix")


def at(day: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=PHOENIX)


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


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"working_window": "08:00-22:00", "working_days": ["mon", "tue", "wed",
                                                                  "thu", "fri", "sat", "sun"]}
    )


class TestItAsksAboutWhatItCannotSettle:
    def test_two_things_at_one_hour_become_a_question(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The owner's real 2026-08-24: LIA 101 10:10–11:00 against BIO 181 10:30–11:45.
        Stale timetable, dropped section, or a registration clash — nothing in the ledger
        can tell, and the difference matters before term starts."""
        a_class(conn, MONDAY, "10:10", "11:00", "LIA 101")
        a_class(conn, MONDAY, "10:30", "11:45", "BIO 181")

        found = questions.detect(conn, sett, MONDAY)

        conflicts = [q for q in found if q.kind == "conflict"]
        assert len(conflicts) == 1
        assert "LIA 101" in conflicts[0].detail and "BIO 181" in conflicts[0].detail
        assert "Attending LIA 101" in conflicts[0].options
        assert "Neither is right" in conflicts[0].options

    def test_back_to_back_classes_are_not_a_conflict(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        a_class(conn, MONDAY, "09:00", "10:00", "CIS 236")
        a_class(conn, MONDAY, "10:00", "11:00", "BIO 181")
        assert [q for q in questions.detect(conn, sett, MONDAY) if q.kind == "conflict"] == []

    def test_a_timetable_clash_is_one_question_however_many_weeks_it_repeats(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Asking about the same two classes on the 24th, the 31st and the 7th is asking
        one question three times, and a surface that does that is one nobody opens twice."""
        for week in range(3):
            day = MONDAY + timedelta(days=7 * week)
            a_class(conn, day, "10:10", "11:00", "LIA 101")
            a_class(conn, day, "10:30", "11:45", "BIO 181")

        conflicts = [q for q in questions.detect(conn, sett, MONDAY) if q.kind == "conflict"]
        assert len(conflicts) == 1
        assert "3 days" in conflicts[0].question

    def test_an_hour_with_no_name_is_asked_about_not_silently_spent(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        a_class(conn, MONDAY, "09:00", "10:00", "New Event")
        untitled = [q for q in questions.detect(conn, sett, MONDAY) if q.kind == "untitled"]
        assert len(untitled) == 1
        assert "60 minutes" in untitled[0].question

    def test_a_placeholder_never_becomes_a_conflict_question(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """"Is CIS 236 or New Event real?" invites an answer about the wrong thing. The
        owner cannot choose between a class and a name Calendar.app invented, and the
        placeholder already has its own, better question."""
        a_class(conn, MONDAY, "09:00", "10:15", "CIS 236")
        a_class(conn, MONDAY, "09:00", "10:00", "New Event")

        found = questions.detect(conn, sett, MONDAY)
        assert [q for q in found if q.kind == "conflict"] == []
        assert len([q for q in found if q.kind == "untitled"]) == 1

    def test_one_detector_failing_does_not_take_the_surface_down(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Rule 5, applied to questions: a morning with no questions because something
        threw is indistinguishable from a morning with nothing to ask."""
        a_class(conn, MONDAY, "09:00", "10:00", "New Event")

        def boom(*args: object, **kwargs: object) -> list[questions.Question]:
            raise RuntimeError("detector down")

        monkeypatch.setattr(questions, "_conflicts", boom)
        assert [q.kind for q in questions.detect(conn, sett, MONDAY)] == ["untitled"]


class TestItAsksOnce:
    def test_refreshing_twice_does_not_ask_again(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        a_class(conn, MONDAY, "10:10", "11:00", "LIA 101")
        a_class(conn, MONDAY, "10:30", "11:45", "BIO 181")

        first = questions.refresh(conn, sett, MONDAY)
        second = questions.refresh(conn, sett, MONDAY)

        assert first >= 1
        assert second == 0
        assert len(questions.open_questions(conn)) == first

    def test_an_answered_question_never_returns(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        a_class(conn, MONDAY, "10:10", "11:00", "LIA 101")
        a_class(conn, MONDAY, "10:30", "11:45", "BIO 181")
        questions.refresh(conn, sett, MONDAY)
        asked = questions.open_questions(conn)[0]

        questions.answer(conn, sett, int(asked["id"]), option="Attending LIA 101")
        questions.refresh(conn, sett, MONDAY)

        assert all(q["id"] != asked["id"] for q in questions.open_questions(conn))


class TestTheOwnersOwnWordsAreACompleteAnswer:
    def _one(self, conn: sqlite3.Connection, sett: Settings) -> int:
        a_class(conn, MONDAY, "10:10", "11:00", "LIA 101")
        a_class(conn, MONDAY, "10:30", "11:45", "BIO 181")
        questions.refresh(conn, sett, MONDAY)
        return int(questions.open_questions(conn)[0]["id"])

    def test_free_text_alone_answers_it(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The options are the cases a detector thought of. The fifth case the owner knows
        about is usually the true one, so the box is never secondary to the buttons."""
        qid = self._one(conn, sett)
        questions.answer(conn, sett, qid, text="I swapped into the Tuesday section last week")

        answered = conn.execute(
            "SELECT status, answer_text, answer_option FROM open_question WHERE id = ?", (qid,)
        ).fetchone()
        assert answered["status"] == "answered"
        assert "Tuesday section" in answered["answer_text"]
        assert answered["answer_option"] is None

    def test_an_answer_outlives_the_question_as_a_decision(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """`decisions` already carries supersession and provenance in words, so the answer
        is recorded where the owner's settled choices live rather than only here."""
        qid = self._one(conn, sett)
        questions.answer(conn, sett, qid, text="Dropped LIA 101")

        titles = {d.choice for d in decisions.active(conn)}
        assert "Dropped LIA 101" in titles

    def test_an_empty_answer_is_refused(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        qid = self._one(conn, sett)
        with pytest.raises(ValueError):
            questions.answer(conn, sett, qid)

    def test_dismissing_settles_nothing_and_still_stops_asking(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Distinct from answered on purpose: waving a question away is not a ruling, and
        recording it as one would put words in the owner's mouth."""
        qid = self._one(conn, sett)
        questions.dismiss(conn, qid)

        assert questions.open_questions(conn) == []
        assert decisions.active(conn) == []
