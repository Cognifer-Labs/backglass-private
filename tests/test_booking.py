"""Reading an assignment as a booking, and moving its deadline to the meeting it precedes.

The fixtures are the real rows. `_DESCRIPTION` is assignment 30's own text, asterisks and
all, because the asterisks are the bug: `instr(description, 'before coming to lab')`
returns 0 against the live ledger and a matcher that ran on raw text would have found
nothing while looking exactly like a matcher with nothing to find.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from backglass import booking
from backglass.config import Settings
from backglass.db import now_iso
from backglass.facts import remember
from backglass.ledger import USER_ID

#: Assignment 30, verbatim from the ledger — the emphasis markers are inside the phrase.
_DESCRIPTION = (
    "Click the button at the bottom of this page and follow the prompts to schedule your "
    "Dreamscape Learn (DSL) Pod Session. You must complete your VR Pod Experience "
    "**before** coming to lab, during its assigned week.\n\n"
    "Spots fill up quickly! Schedule your DSL Pod session for each Act right away.\n"
    "Contact [DSLsupport@asu.edu](mailto:DSLsupport@asu.edu) with questions."
)

_TITLE = "Schedule Here! Lab 2 Module 1 - Act I: The Chameleon Code - VR Pod Session"


def _assignment(
    conn: sqlite3.Connection,
    *,
    title: str = _TITLE,
    course: str = "2026FallC-T-CHM113-LABORATORY",
    due_at: str | None = "2026-09-03",
    description: str = _DESCRIPTION,
    effort: int | None = 90,
) -> int:
    cur = conn.execute(
        "INSERT INTO assignment (user_id, source, external_id, course, title, due_at,"
        " description, description_hash, effort_minutes, first_seen_at, last_changed_at)"
        " VALUES (?, 'canvas:ics', ?, ?, ?, ?, ?, 'h', ?, ?, ?)",
        (USER_ID, f"assignment:{title[:20]}", course, title, due_at, description,
         effort, now_iso(), now_iso()),
    )
    return int(cur.lastrowid or 0)


def _calendar_meeting(conn: sqlite3.Connection, title: str, starts: str, ends: str) -> int:
    payload = json.dumps({"starts_at": starts, "ends_at": ends, "course": title.split(" (")[0]})
    cur = conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict, raw_json)"
        " VALUES (?, 'calendar:asu', ?, ?, ?, ?, '', ?, 'keep', ?)",
        (USER_ID, f"cal:{title}:{starts}", now_iso(), starts, title,
         f"h-{title}-{starts}", payload),
    )
    return int(cur.lastrowid or 0)


class TestNormalise:
    def test_emphasis_inside_a_phrase_does_not_break_it(self) -> None:
        """The whole reason this module normalises before it matches."""
        assert "before coming to lab" not in _DESCRIPTION
        assert "before coming to lab" in booking.normalise(_DESCRIPTION)

    def test_emphasis_is_stripped_not_spaced(self) -> None:
        assert booking.normalise("a **b** c") == "a b c"

    @pytest.mark.parametrize("raw", ["a\xa0b", "a​b", "a　b"])
    def test_the_invisible_spaces_collapse(self, raw: str) -> None:
        assert booking.normalise(raw) == "a b"

    def test_a_link_keeps_its_words_and_loses_its_url(self) -> None:
        assert booking.normalise("mail [DSL](mailto:x@y.z) now") == "mail dsl now"


class TestDetect:
    @pytest.mark.parametrize(
        "title",
        [
            _TITLE,
            "Sign Up for your advising appointment",
            "RSVP: Share-a-thon",
            "Reserve a study room",
        ],
    )
    def test_the_verb_in_the_title_is_the_signal(self, title: str) -> None:
        assert booking.detect(title=title, description="")

    def test_the_body_alone_is_never_enough(self) -> None:
        """"Schedule your reading for the week" is prose about planning, not a portal.

        Precision is what makes a regex safe to run where a model is not being asked, so
        the body may corroborate a title and may not stand in for one.
        """
        assert not booking.detect(
            title="Week 3 Reading", description="schedule your reading around the pod session"
        )

    def test_a_plain_assignment_is_not_a_booking(self) -> None:
        assert not booking.detect(
            title="LearningCurve Chapter 4", description="Complete the adaptive quiz."
        )

    @pytest.mark.parametrize(
        "title",
        ["Reserve Reading: Chapter 4", "Book Review Essay", "Booked Solid: a case"],
    )
    def test_the_weak_verbs_need_an_object(self, title: str) -> None:
        """`reserve` and `book` have ordinary English meanings a course catalogue is full
        of. A shelf is not a portal and a book review is not a reservation, so those two
        verbs only count with `a|an|your|the` after them — which every real signup title
        has and none of these do."""
        assert not booking.detect(title=title, description="")


class TestCourseOf:
    @pytest.mark.parametrize(
        ("feed", "expected"),
        [
            ("2026FallC-T-CHM113-LABORATORY", ("CHM 113", "lab")),
            ("2026FallC-T-CHM113-Recitation", ("CHM 113", "recitation")),
            ("2026FallC-T-BIO181-60069", ("BIO 181", "")),
            ("TRN-ASUReady-UG", ("", "")),
        ],
    )
    def test_the_feed_string_yields_a_code(self, feed: str, expected: tuple[str, str]) -> None:
        assert booking.course_of(feed) == expected


class TestOperativeDeadline:
    def test_a_silent_description_keeps_the_canvas_date(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        deadline = booking.operative_deadline(
            conn, settings, course="CHM 113", component="lab", due_at="2026-09-03",
            description="Complete the pre-lab worksheet.",
        )
        assert deadline.at == "2026-09-03"
        assert deadline.basis == "due_at"
        assert not deadline.moved

    def test_no_meeting_anywhere_keeps_the_canvas_date(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Silence is a legitimate answer and must not read as a moved deadline."""
        deadline = booking.operative_deadline(
            conn, settings, course="CHM 113", component="lab", due_at="2026-09-03",
            description=_DESCRIPTION,
        )
        assert deadline.basis == "due_at"

    def test_the_calendar_answers_when_no_override_exists(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _calendar_meeting(conn, "CHM 113 (Lab)", "2026-09-03T18:00:00-07:00",
                          "2026-09-03T19:50:00-07:00")
        deadline = booking.operative_deadline(
            conn, settings, course="CHM 113", component="lab", due_at="2026-09-03",
            description=_DESCRIPTION,
        )
        assert deadline.at == "2026-09-03T18:00:00-07:00"
        assert deadline.basis == "meeting:lab:calendar"
        assert deadline.moved
        assert deadline.conflict == ""

    def test_the_fact_override_wins_and_states_the_disagreement(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The case the module exists for.

        `calendar:asu` still carries the pre-swap Thursday-evening lab. The owner's own
        correction says Thursday morning. Trusting the calendar returns a deadline
        sixteen hours late — for the one assignment family that motivated the work.
        """
        _calendar_meeting(conn, "CHM 113 (Lab)", "2026-09-03T18:00:00-07:00",
                          "2026-09-03T19:50:00-07:00")
        remember(conn, settings, "education", "meeting:chm113:lab", "Thu 08:00-09:50")

        deadline = booking.operative_deadline(
            conn, settings, course="CHM 113", component="lab", due_at="2026-09-03",
            description=_DESCRIPTION,
        )
        assert deadline.at is not None and deadline.at.startswith("2026-09-03T08:00")
        assert deadline.basis == "meeting:lab:fact"
        assert "fact says Thu 08:00" in deadline.conflict
        assert "calendar:asu says Thu 18:00" in deadline.conflict

    def test_an_agreeing_calendar_raises_no_conflict(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _calendar_meeting(conn, "CHM 113 (Lab)", "2026-09-03T08:00:00-07:00",
                          "2026-09-03T09:50:00-07:00")
        remember(conn, settings, "education", "meeting:chm113:lab", "Thu 08:00-09:50")
        deadline = booking.operative_deadline(
            conn, settings, course="CHM 113", component="lab", due_at="2026-09-03",
            description=_DESCRIPTION,
        )
        assert deadline.conflict == ""
        assert deadline.moved

    def test_a_malformed_override_is_ignored_rather_than_obeyed(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _calendar_meeting(conn, "CHM 113 (Lab)", "2026-09-03T18:00:00-07:00",
                          "2026-09-03T19:50:00-07:00")
        remember(conn, settings, "education", "meeting:chm113:lab", "thursday mornings")
        deadline = booking.operative_deadline(
            conn, settings, course="CHM 113", component="lab", due_at="2026-09-03",
            description=_DESCRIPTION,
        )
        assert deadline.basis == "meeting:lab:calendar"

    def test_a_retracted_meeting_is_not_the_deadline(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Load-bearing on the live ledger, and the reason the scan came out right.

        `calendar:asu` holds both the pre-swap Thursday-evening lab and the corrected
        Thursday-morning one for every week of the semester; the evening rows carry an
        owner retraction. Picking "the last meeting at or before the due date" without
        honouring retractions returns 18:00 every time, because 18:00 is later than
        08:00 on the same day — the wrong answer, arrived at confidently.
        """
        stale = _calendar_meeting(conn, "CHM 113 (Lab)", "2026-09-03T18:00:00-07:00",
                                  "2026-09-03T19:50:00-07:00")
        _calendar_meeting(conn, "CHM 113 (Lab)", "2026-09-03T08:00:00-07:00",
                          "2026-09-03T09:50:00-07:00")
        conn.execute(
            "INSERT INTO source_item_retraction (source_item_id, reason, retracted_at)"
            " VALUES (?, 'Owner marked this not happening', ?)",
            (stale, now_iso()),
        )
        deadline = booking.operative_deadline(
            conn, settings, course="CHM 113", component="lab", due_at="2026-09-03",
            description=_DESCRIPTION,
        )
        assert deadline.at == "2026-09-03T08:00:00-07:00"

    def test_a_meeting_outside_the_lookback_is_not_reached_for(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _calendar_meeting(conn, "CHM 113 (Lab)", "2026-07-02T08:00:00-07:00",
                          "2026-07-02T09:50:00-07:00")
        deadline = booking.operative_deadline(
            conn, settings, course="CHM 113", component="lab", due_at="2026-09-03",
            description=_DESCRIPTION,
        )
        assert deadline.basis == "due_at"


class TestScan:
    def test_the_ninety_minutes_becomes_a_booking_and_an_appointment(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _assignment(conn)
        report = booking.scan(conn, settings)
        assert report.matched == 1
        found = report.bookings[0]
        assert found.book_minutes == booking.BOOK_MINUTES
        assert found.attend_minutes == 90
        assert found.course == "CHM 113"

    def test_the_counters_speak_when_nothing_matched(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A detector that finds nothing has to say it looked."""
        _assignment(conn, title="LearningCurve Chapter 4", description="Adaptive quiz.")
        report = booking.scan(conn, settings)
        assert report.scanned == 1 and report.matched == 0
        assert "1 assignment(s) scanned, 0 read as bookings" in report.lines()[0]

    def test_a_conflict_reaches_the_report(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _assignment(conn)
        _calendar_meeting(conn, "CHM 113 (Lab)", "2026-09-03T18:00:00-07:00",
                          "2026-09-03T19:50:00-07:00")
        remember(conn, settings, "education", "meeting:chm113:lab", "Thu 08:00-09:50")
        report = booking.scan(conn, settings)
        assert report.deadlines_moved == 1
        assert len(report.conflicts) == 1
        assert any("conflict:" in line for line in report.lines())
