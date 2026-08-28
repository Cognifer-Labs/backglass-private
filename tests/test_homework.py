"""The Homework month: the grid, the day an item lands on, and the count underneath.

Three of these are regression tests for failures the design already knows about. A Canvas
deadline is stored as the UTC instant `2026-09-03T06:59:59Z`, which is 23:59 on the 2nd in
Phoenix — bucket it by its own string and every Canvas row on the page is a day late. An
assignment with no commitment behind it was invisible on every surface for as long as
nothing counted the two against each other (2026-08-27). And one class arriving from two
calendars is one class, which is what `_distinct` exists for and what a month grid would
otherwise draw twice a day, thirty times a month.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from backglass import homework
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.web.app import create_app

PHOENIX = ZoneInfo("America/Phoenix")
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=PHOENIX)
TODAY = date(2026, 9, 10)


def _item(conn: sqlite3.Connection, *, source: str, external: str, title: str) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, 'keep')",
        (USER_ID, source, external, now_iso(), "2026-09-01T12:00:00+00:00",
         title, title, f"h-{external}"),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _assignment(
    conn: sqlite3.Connection,
    *,
    external: str,
    title: str,
    due_at: str,
    course: str = "2026FallC-T-CHM113-LABORATORY",
    item_id: int | None = None,
    minutes: int | None = 30,
) -> int:
    conn.execute(
        "INSERT INTO assignment (user_id, source, external_id, source_item_id, course,"
        " title, due_at, url, description, description_hash, effort_minutes,"
        " first_seen_at, last_changed_at)"
        " VALUES (?, 'canvas:ics', ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)",
        (USER_ID, external, item_id, course, title, due_at,
         f"https://canvas.example/{external}", f"d-{external}", minutes,
         now_iso(), now_iso()),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _commitment(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    what: str,
    due_at: str,
    status: str = "open",
    minutes: int | None = 30,
) -> int:
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes,"
        " confidence, status, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, ?, ?, 0.9, ?, ?, ?)",
        (USER_ID, what, due_at, minutes, status, item_id, now_iso()),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _calendar(
    conn: sqlite3.Connection, *, source: str, external: str, title: str,
    starts_at: str, ends_at: str,
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash)"
        " VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
        (USER_ID, source, external, now_iso(), starts_at, title,
         json.dumps({"starts_at": starts_at, "ends_at": ends_at, "status": "confirmed"}),
         f"h-{external}"),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _load(conn: sqlite3.Connection, settings: Settings, **kwargs: object) -> homework.Month:
    return homework.load(
        conn, settings, date(2026, 9, 1), today=TODAY, now=NOW, **kwargs  # type: ignore[arg-type]
    )


def _cells(month: homework.Month) -> dict[date, homework.Day]:
    return {day.day: day for week in month.weeks for day in week}


def test_the_grid_is_monday_first_and_covers_the_whole_month(conn, settings) -> None:  # type: ignore[no-untyped-def]
    month = _load(conn, settings)
    assert [d.day for d in month.weeks[0]][0] == date(2026, 8, 31)
    assert month.weeks[0][0].day.weekday() == 0
    assert month.weeks[-1][-1].day.weekday() == 6
    days = _cells(month)
    assert date(2026, 9, 30) in days
    assert days[date(2026, 8, 31)].in_month is False
    assert days[date(2026, 9, 1)].in_month is True


def test_the_pager_walks_months_and_wraps_the_year(conn, settings) -> None:  # type: ignore[no-untyped-def]
    assert homework.shift(date(2026, 1, 1), -1) == date(2025, 12, 1)
    assert homework.shift(date(2026, 12, 1), 1) == date(2027, 1, 1)
    month = _load(conn, settings)
    assert (month.prev, month.next) == (date(2026, 8, 1), date(2026, 10, 1))


def test_a_utc_deadline_lands_on_the_day_the_owner_would_call_it(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """`2026-09-03T06:59:59Z` is 23:59 on the 2nd in Phoenix, and the 2nd is where it goes.

    Bucketing on the stored string would file every Canvas deadline a day late — for the
    whole page, every month, silently.
    """
    item = _item(conn, source="canvas:ics", external="a1", title="Prelab Quiz 1")
    _assignment(conn, external="a1", title="Prelab Quiz 1",
                due_at="2026-09-03T06:59:59Z", item_id=item)
    _commitment(conn, item_id=item, what="Complete Prelab Quiz 1",
                due_at="2026-09-03T06:59:59Z")

    days = _cells(_load(conn, settings))
    assert [q.title for q in days[date(2026, 9, 2)].due] == ["Complete Prelab Quiz 1"]
    assert days[date(2026, 9, 3)].due == []


def test_an_assignment_with_a_commitment_is_drawn_once(conn, settings) -> None:  # type: ignore[no-untyped-def]
    item = _item(conn, source="canvas:ics", external="a2", title="Post-Lab 1")
    _assignment(conn, external="a2", title="Post-Lab 1", due_at="2026-09-15", item_id=item)
    _commitment(conn, item_id=item, what="Submit Post-Lab 1", due_at="2026-09-15")

    month = _load(conn, settings)
    entries = _cells(month)[date(2026, 9, 15)].due
    assert [(q.kind, q.title) for q in entries] == [("commitment", "Submit Post-Lab 1")]
    # The commitment borrows the assignment's course and its Canvas link, which is where
    # the work is actually done.
    assert entries[0].course == "CHM 113 (Lab)"
    assert entries[0].href == "https://canvas.example/a2"
    assert month.counts == homework.Counts(assignments=1, linked=1, unlinked=0)


def test_an_assignment_with_nothing_extracted_from_it_is_drawn_and_counted(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The 2026-08-27 failure, as a test. Silence is the thing being guarded against."""
    item = _item(conn, source="canvas:ics", external="a3", title="In-Lab 1")
    _assignment(conn, external="a3", title="In-Lab 1", due_at="2026-09-16", item_id=item)

    month = _load(conn, settings)
    entries = _cells(month)[date(2026, 9, 16)].due
    assert [(q.kind, q.title) for q in entries] == [("unlinked", "In-Lab 1")]
    assert month.counts == homework.Counts(assignments=1, linked=0, unlinked=1)
    assert [q.title for q in month.unlinked] == ["In-Lab 1"]


def test_an_unlinked_assignment_outside_the_month_is_drawn_but_not_counted(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The grid runs to a Sunday; the count is about September. Two spans, two answers."""
    item = _item(conn, source="canvas:ics", external="a4", title="August leftover")
    _assignment(conn, external="a4", title="August leftover", due_at="2026-08-31",
                item_id=item)

    month = _load(conn, settings)
    assert [q.title for q in _cells(month)[date(2026, 8, 31)].due] == ["August leftover"]
    assert month.counts == homework.Counts(assignments=0, linked=0, unlinked=0)
    assert month.unlinked == []


def test_overdue_is_measured_against_the_injected_clock(conn, settings) -> None:  # type: ignore[no-untyped-def]
    item = _item(conn, source="apple-mail", external="m1", title="RSVP")
    _commitment(conn, item_id=item, what="RSVP for the CHM 113 review session",
                due_at="2026-09-09T17:00:00")
    later = _item(conn, source="apple-mail", external="m2", title="Form")
    _commitment(conn, item_id=later, what="Fill out the CHM 113 form",
                due_at="2026-09-11T17:00:00")

    days = _cells(_load(conn, settings))
    assert days[date(2026, 9, 9)].due[0].overdue is True
    assert days[date(2026, 9, 11)].due[0].overdue is False


def test_a_finished_commitment_stays_on_the_month_marked_done(conn, settings) -> None:  # type: ignore[no-untyped-def]
    item = _item(conn, source="apple-mail", external="m3", title="Survey")
    _commitment(conn, item_id=item, what="Complete the CHM 113 survey",
                due_at="2026-09-04", status="done")

    entry = _cells(_load(conn, settings))[date(2026, 9, 4)].due[0]
    assert (entry.kind, entry.done, entry.overdue) == ("done", True, False)


def test_coursework_only_drops_what_names_no_course(conn, settings) -> None:  # type: ignore[no-untyped-def]
    course = _item(conn, source="apple-mail", external="m4", title="Quiz")
    _commitment(conn, item_id=course, what="Complete the CHM 113 safety quiz",
                due_at="2026-09-14")
    other = _item(conn, source="apple-mail", external="m5", title="Dentist")
    _commitment(conn, item_id=other, what="Book a dentist appointment", due_at="2026-09-14")

    both = _cells(_load(conn, settings))[date(2026, 9, 14)].due
    assert len(both) == 2
    only = _cells(_load(conn, settings, only_coursework=True))[date(2026, 9, 14)].due
    assert [q.course for q in only] == ["CHM 113"]


def test_one_class_from_two_calendars_is_drawn_once(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The same lecture, spelled two ways. `_distinct` compares instants, not strings."""
    _calendar(conn, source="calendar:asu", external="c1", title="CHM 113",
              starts_at="2026-09-14T12:20:00-07:00", ends_at="2026-09-14T13:10:00-07:00")
    _calendar(conn, source="calendar:apple", external="c2", title="CHM 113",
              starts_at="2026-09-14T19:20:00.000Z", ends_at="2026-09-14T20:10:00.000Z")

    events = _cells(_load(conn, settings))[date(2026, 9, 14)].events
    assert [(e.title, e.time_label) for e in events] == [("CHM 113", "12:20")]


def test_a_retracted_calendar_row_leaves_the_grid(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The lab that moved on 2026-08-23 and was still drawn four days later."""
    item = _calendar(conn, source="calendar:apple", external="c3", title="CHM 113 (Lab)",
                     starts_at="2026-09-17T18:00:00.000Z",
                     ends_at="2026-09-17T19:50:00.000Z")
    assert _cells(_load(conn, settings))[date(2026, 9, 17)].events != []
    conn.execute(
        "INSERT INTO source_item_retraction (user_id, source_item_id, retracted_at, reason)"
        " VALUES (?, ?, ?, 'gone from the feed')",
        (USER_ID, item, now_iso()),
    )
    conn.commit()
    assert _cells(_load(conn, settings))[date(2026, 9, 17)].events == []


def test_the_page_renders_and_states_what_it_was_built_from(conn, settings) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import panel_slice

    item = _item(conn, source="canvas:ics", external="a9", title="In-Lab 2")
    _assignment(conn, external="a9", title="In-Lab 2", due_at="2026-09-16", item_id=item)

    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    body = client.get("/homework?month=2026-09").text
    grid = panel_slice(body, "panel-homework-grid")
    assert "In-Lab 2" in grid
    audit = panel_slice(body, "panel-homework-audit")
    assert "1</b> Canvas assignments due this month" in audit
    assert "0</b> with a commitment behind them" in audit
    assert "canvas:ics" in audit  # the bound of the measurement, stated with it


def test_a_malformed_month_falls_back_rather_than_failing(conn, settings) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    assert client.get("/homework?month=2026-13").status_code == 200
    assert client.get("/homework?month=9999-12").status_code == 200
    assert client.get("/homework?month=nonsense").status_code == 422


def test_a_dated_but_untimed_deadline_still_reads_as_late(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Most of the ledger's deadlines carry no hour, and an instant-only comparison never
    fires for them — no ▲ and no vermilion on the one page whose point is lateness."""
    late = _item(conn, source="canvas:ics", external="a5", title="Syllabus quiz")
    _assignment(conn, external="a5", title="Syllabus quiz", due_at="2026-09-08",
                item_id=late)
    _commitment(conn, item_id=late, what="Complete the CHM 113 syllabus quiz",
                due_at="2026-09-08")
    today_item = _item(conn, source="canvas:ics", external="a6", title="Reading quiz")
    _assignment(conn, external="a6", title="Reading quiz", due_at="2026-09-10",
                item_id=today_item)
    _commitment(conn, item_id=today_item, what="Complete the CHM 113 reading quiz",
                due_at="2026-09-10")

    days = _cells(_load(conn, settings))
    assert days[date(2026, 9, 8)].due[0].overdue is True
    # Due today is not late yet: an untimed deadline is due by the end of its day.
    assert days[date(2026, 9, 10)].due[0].overdue is False


def test_an_unlinked_assignment_is_late_on_the_same_rule(conn, settings) -> None:  # type: ignore[no-untyped-def]
    item = _item(conn, source="canvas:ics", external="a7", title="In-Lab 0")
    _assignment(conn, external="a7", title="In-Lab 0", due_at="2026-09-04", item_id=item)

    entry = _cells(_load(conn, settings))[date(2026, 9, 4)].due[0]
    assert (entry.kind, entry.overdue) == ("unlinked", True)
