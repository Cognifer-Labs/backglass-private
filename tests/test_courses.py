"""The semester reader: how three vocabularies for one course become one card.

The cases that matter here are the ones that were wrong before the tests existed: a
sentence shaped like a course code inventing a class, and a Canvas-only shell or a
calendar-only class being dropped because the other half was missing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from backglass import courses
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID

PHOENIX = ZoneInfo("America/Phoenix")
NOW = datetime(2026, 8, 21, 9, 0, tzinfo=PHOENIX)


def _calendar_row(
    conn: sqlite3.Connection,
    *,
    source: str,
    external: str,
    title: str,
    starts_at: str,
    ends_at: str,
    payload: dict[str, object] | None = None,
) -> int:
    body = {"starts_at": starts_at, "ends_at": ends_at, "status": "confirmed"}
    body.update(payload or {})
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash)"
        " VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
        (
            USER_ID,
            source,
            external,
            now_iso(),
            starts_at,
            title,
            json.dumps(body),
            f"h-{external}",
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _lecture_week(conn: sqlite3.Connection) -> None:
    """CHM 113 as the ASU feed writes it: one row per meeting, three days a week."""
    for n, day in enumerate(("24", "26", "28")):
        _calendar_row(
            conn,
            source="calendar:asu",
            external=f"chm-{day}",
            title="CHM 113",
            starts_at=f"2026-08-{day}T12:20:00-07:00",
            ends_at=f"2026-08-{day}T13:10:00-07:00",
            payload={
                "course": "CHM 113",
                "location": "Tempe LSA 191",
                "instructors": ["Wei Wang"],
            },
        )
        del n


class TestMeetings:
    def test_a_semester_of_rows_folds_into_one_weekly_line(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _lecture_week(conn)
        found = courses.load(conn, settings, now=NOW)
        assert [c.subject for c in found] == ["CHM 113"]
        meeting = found[0].meetings[0]
        assert meeting.component == "Lecture"
        assert meeting.days == "Mon/Wed/Fri"
        assert meeting.when == "Mon/Wed/Fri 12:20 pm–1:10 pm"
        assert meeting.room == "Tempe LSA 191"
        assert meeting.instructors == ["Wei Wang"]
        # The count is on the page so a wrong-looking pattern can be re-derived.
        assert meeting.occurrences == 3

    def test_the_lab_is_its_own_component_of_the_same_course(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _lecture_week(conn)
        _calendar_row(
            conn,
            source="calendar:asu",
            external="chm-lab",
            title="CHM 113 (Lab)",
            starts_at="2026-08-27T18:00:00-07:00",
            ends_at="2026-08-27T19:50:00-07:00",
            payload={
                "course": "CHM 113 (Lab)",
                "location": "Tempe PSD 232",
                "instructors": ["Beatriz Smith"],
            },
        )
        found = courses.load(conn, settings, now=NOW)
        assert len(found) == 1, "a lab is a component, never a second course"
        components = [m.component for m in found[0].meetings]
        assert components == ["Lecture", "Lab"], "the lecture leads its own card"
        assert found[0].rooms == ["Tempe LSA 191", "Tempe PSD 232"]

    def test_a_retracted_meeting_is_not_read(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _lecture_week(conn)
        dropped = _calendar_row(
            conn,
            source="calendar:asu",
            external="chm-gone",
            title="CHM 113",
            starts_at="2026-09-02T12:20:00-07:00",
            ends_at="2026-09-02T13:10:00-07:00",
            payload={"course": "CHM 113", "location": "Tempe LSA 191"},
        )
        conn.execute(
            "INSERT INTO source_item_retraction (user_id, source_item_id, retracted_at,"
            " reason) VALUES (?, ?, ?, 'absent from re-read')",
            (USER_ID, dropped, now_iso()),
        )
        conn.commit()
        found = courses.load(conn, settings, now=NOW)
        assert found[0].meetings[0].occurrences == 3


class TestWhatCountsAsACourse:
    def test_a_room_number_in_a_commitment_is_not_a_class(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """"Meet me in Hall 502" matches the course-code regex and is not a course.

        It appeared as one until subjects were restricted to what the registrar or Canvas
        already vouches for — the exact failure this test exists to keep out.
        """
        _lecture_week(conn)
        item = _calendar_row(
            conn,
            source="calendar:asu",
            external="chm-24b",
            title="CHM 113",
            starts_at="2026-08-31T12:20:00-07:00",
            ends_at="2026-08-31T13:10:00-07:00",
            payload={"course": "CHM 113"},
        )
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
            " source_item_id, created_at) VALUES (?, 'i_owe', ?, '2026-08-30', 0.9, 'open',"
            " ?, ?)",
            (USER_ID, "Meet the advisor in Hall 502", item, now_iso()),
        )
        conn.commit()
        found = courses.load(conn, settings, now=NOW)
        assert [c.subject for c in found] == ["CHM 113"]

    def test_a_commitment_naming_a_real_course_attaches_to_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _lecture_week(conn)
        item = _calendar_row(
            conn,
            source="calendar:asu",
            external="chm-31",
            title="CHM 113",
            starts_at="2026-08-31T12:20:00-07:00",
            ends_at="2026-08-31T13:10:00-07:00",
            payload={"course": "CHM 113"},
        )
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
            " source_item_id, created_at) VALUES (?, 'i_owe', ?, '2026-09-23', 0.9, 'open',"
            " ?, ?)",
            (USER_ID, "Sit CHM 113 Exam 1 (Modules 1-4)", item, now_iso()),
        )
        conn.commit()
        found = courses.load(conn, settings, now=NOW)
        assert [o.what for o in found[0].obligations] == [
            "Sit CHM 113 Exam 1 (Modules 1-4)"
        ]
        assert found[0].obligations[0].source_item_id == item, "rule 1: evidence attached"


class TestSyllabusDates:
    def test_an_exam_written_to_the_syllabus_calendar_lands_on_its_course(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _lecture_week(conn)
        exam = _calendar_row(
            conn,
            source="calendar:apple",
            external="chm113-exam1",
            title="CHM 113 — Exam 1 (Modules 1–4)",
            starts_at="2026-09-23T12:20:00-07:00",
            ends_at="2026-09-23T13:10:00-07:00",
            payload={"calendar": courses.SYLLABUS_CALENDAR, "location": "Tempe LSA 191"},
        )
        found = courses.load(conn, settings, now=NOW)
        assert [d.title for d in found[0].dates] == ["CHM 113 — Exam 1 (Modules 1–4)"]
        assert found[0].dates[0].source_item_id == exam
        assert found[0].dates[0].time_label == "12:20 pm"

    def test_an_ordinary_calendar_event_is_a_meeting_not_a_date(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Only the syllabus calendar produces dates. A CHM 113 row on the Family
        calendar is the class itself, and listing it as an exam would double it."""
        _lecture_week(conn)
        _calendar_row(
            conn,
            source="calendar:apple",
            external="chm-family",
            title="CHM 113",
            starts_at="2026-09-23T12:20:00-07:00",
            ends_at="2026-09-23T13:10:00-07:00",
            payload={"calendar": "Family"},
        )
        found = courses.load(conn, settings, now=NOW)
        assert found[0].dates == []

    def test_a_date_that_has_passed_is_not_upcoming(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _lecture_week(conn)
        _calendar_row(
            conn,
            source="calendar:apple",
            external="chm113-old",
            title="CHM 113 — Exam 0",
            starts_at="2026-08-19T12:20:00-07:00",
            ends_at="2026-08-19T13:10:00-07:00",
            payload={"calendar": courses.SYLLABUS_CALENDAR},
        )
        found = courses.load(conn, settings, now=NOW)
        assert found[0].dates == []


class TestCoursework:
    def test_canvas_assignments_group_under_the_course_they_belong_to(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _lecture_week(conn)
        conn.execute(
            "INSERT INTO assignment (user_id, source, external_id, course, title, due_at,"
            " description_hash, effort_minutes, effort_basis, effort_quote, sessions,"
            " first_seen_at, last_changed_at)"
            " VALUES (?, 'canvas:ics', 'assignment:1', '2026FallC-T-CHM113-60105',"
            " 'Module 1 homework', '2026-08-30', 'h1', 45, 'type:homework', '', 1, ?, ?)",
            (USER_ID, now_iso(), now_iso()),
        )
        conn.execute(
            "INSERT INTO assignment (user_id, source, external_id, course, title, due_at,"
            " description_hash, first_seen_at, last_changed_at)"
            " VALUES (?, 'canvas:ics', 'assignment:2', 'TRN-ASUReady-UG', 'Orientation',"
            " '2026-09-04', 'h2', ?, ?)",
            (USER_ID, now_iso(), now_iso()),
        )
        conn.commit()
        found = courses.load(conn, settings, now=NOW)
        assert [c.subject for c in found] == ["CHM 113"], "an org shell is not a class"
        assert found[0].assignment_total == 1
        assert [a.title for a in found[0].upcoming] == ["Module 1 homework"]

    def test_work_already_due_is_not_listed_as_coming(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _lecture_week(conn)
        conn.execute(
            "INSERT INTO assignment (user_id, source, external_id, course, title, due_at,"
            " description_hash, first_seen_at, last_changed_at)"
            " VALUES (?, 'canvas:ics', 'assignment:3', '2026FallC-T-CHM113-60105',"
            " 'Last week', '2026-08-14', 'h3', ?, ?)",
            (USER_ID, now_iso(), now_iso()),
        )
        conn.commit()
        found = courses.load(conn, settings, now=NOW)
        assert found[0].upcoming == []
        assert found[0].assignment_total == 1, "still counted, just not upcoming"


class TestDocuments:
    """Drop-folder files, attached to the course whose folder they sit in."""

    def _file_row(self, conn: sqlite3.Connection, path: str, title: str) -> int:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, raw_json, content_hash)"
            " VALUES (?, 'files', ?, ?, '2026-08-21T09:00:00-07:00', NULL, ?, 'x', '{}', ?)",
            (USER_ID, path, now_iso(), title, f"h-{path}"),
        )
        conn.commit()
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def test_a_course_with_one_component_still_finds_its_folder(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """`CHM113-Lecture` and `HON171` are both course folders. Matching on a bare
        `HON171-` prefix found neither of HON 171's two documents."""
        _lecture_week(conn)
        _calendar_row(
            conn,
            source="calendar:asu",
            external="hon-25",
            title="HON 171",
            starts_at="2026-08-25T10:30:00-07:00",
            ends_at="2026-08-25T11:45:00-07:00",
            payload={"course": "HON 171", "location": "Tempe WILOHAL 112"},
        )
        self._file_row(conn, "HON171/HON 171 Syllabus - Fall 2026.pdf", "HON 171 Syllabus")
        self._file_row(conn, "CHM113-Lecture/CHM113 Syllabus.pdf", "CHM113 Syllabus")
        found = {c.subject: c for c in courses.load(conn, settings, now=NOW)}
        assert [d.path for d in found["HON 171"].documents] == [
            "HON171/HON 171 Syllabus - Fall 2026.pdf"
        ]
        assert [d.path for d in found["CHM 113"].documents] == [
            "CHM113-Lecture/CHM113 Syllabus.pdf"
        ]

    def test_a_file_outside_any_course_folder_attaches_to_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _lecture_week(conn)
        self._file_row(conn, "README.md", "ASU Fall 2026 archive")
        self._file_row(conn, "CHM113x/notes.pdf", "not the course folder")
        found = courses.load(conn, settings, now=NOW)
        assert found[0].documents == []
