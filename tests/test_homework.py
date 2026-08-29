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


# ── the links out, and the links in ──────────────────────────────────────────
#
# A month page nothing points at is a page nobody opens. These hold the three joins the
# page was wired with: the course it can be narrowed to, the plan block that says the
# work has actually been made room for, and the pagers on the other two registers.

def _plan(
    conn: sqlite3.Connection,
    *,
    day: str,
    commitment_id: int,
    status: str = "proposed",
    outcome: str = "pending",
) -> int:
    conn.execute(
        "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes, generated_at,"
        " status) VALUES (?, ?, 'America/Phoenix', 300, ?, ?)",
        (USER_ID, day, now_iso(), status),
    )
    plan = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO plan_block (day_plan_id, user_id, starts_at, ends_at, kind,"
        " commitment_id, title, outcome)"
        " VALUES (?, ?, ?, ?, 'work', ?, 'block', ?)",
        (plan, USER_ID, f"{day}T09:00:00-07:00", f"{day}T09:30:00-07:00",
         commitment_id, outcome),
    )
    conn.commit()
    return plan


def test_a_course_narrows_the_month_to_that_course(conn, settings) -> None:  # type: ignore[no-untyped-def]
    chem = _item(conn, source="canvas:ics", external="c1", title="Prelab")
    _assignment(conn, external="c1", title="Prelab", due_at="2026-09-14", item_id=chem)
    _commitment(conn, item_id=chem, what="Complete the prelab quiz", due_at="2026-09-14")
    bio = _item(conn, source="canvas:ics", external="c2", title="Reading quiz")
    _assignment(conn, external="c2", title="Reading quiz", due_at="2026-09-14",
                course="2026FallC-T-BIO181-60069", item_id=bio)
    _commitment(conn, item_id=bio, what="Complete the BIO 181 reading quiz",
                due_at="2026-09-14")

    both = _cells(_load(conn, settings))[date(2026, 9, 14)].due
    assert len(both) == 2
    only = _cells(_load(conn, settings, course="CHM 113"))[date(2026, 9, 14)].due
    assert [q.course for q in only] == ["CHM 113 (Lab)"]


def test_a_course_keeps_its_own_lab_and_recitation(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The filter is by course and never by component. A student asking what CHM 113
    wants from them this month means the lecture, the lab and the recitation — splitting
    those hides two thirds of the answer behind a chip nobody knew to click."""
    for n, (course, what) in enumerate([
        ("2026FallC-T-CHM113-60105", "Complete the CHM 113 chapter quiz"),
        ("2026FallC-T-CHM113-LABORATORY", "Submit the CHM 113 lab report"),
        ("2026FallC-T-CHM113-Recitation", "Complete the CHM 113 recitation activity"),
    ]):
        item = _item(conn, source="canvas:ics", external=f"k{n}", title=what)
        _assignment(conn, external=f"k{n}", title=what, due_at="2026-09-14",
                    course=course, item_id=item)
        _commitment(conn, item_id=item, what=what, due_at="2026-09-14")

    entries = _cells(_load(conn, settings, course="CHM 113"))[date(2026, 9, 14)].due
    assert sorted(q.course for q in entries) == [
        "CHM 113", "CHM 113 (Lab)", "CHM 113 (Recitation)"
    ]


def test_the_course_is_read_in_any_spelling_a_link_carries(conn, settings) -> None:  # type: ignore[no-untyped-def]
    item = _item(conn, source="canvas:ics", external="c3", title="Prelab")
    _assignment(conn, external="c3", title="Prelab", due_at="2026-09-14", item_id=item)
    _commitment(conn, item_id=item, what="Complete the prelab quiz", due_at="2026-09-14")
    for spelling in ("CHM 113", "chm113", "CHM113", "chm 113"):
        month = _load(conn, settings, course=spelling)
        assert month.course == "CHM 113", spelling
        assert month.slug == "chm113"
        assert _cells(month)[date(2026, 9, 14)].due


def test_the_chip_row_still_names_the_courses_it_filtered_away(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Read off the finished grid this would list one course — the one already chosen —
    and the owner would have no way back to the others."""
    chem = _item(conn, source="canvas:ics", external="c4", title="Prelab")
    _assignment(conn, external="c4", title="Prelab", due_at="2026-09-14", item_id=chem)
    _commitment(conn, item_id=chem, what="Complete the prelab quiz", due_at="2026-09-14")
    bio = _item(conn, source="canvas:ics", external="c5", title="Reading quiz")
    _assignment(conn, external="c5", title="Reading quiz", due_at="2026-09-14",
                course="2026FallC-T-BIO181-60069", item_id=bio)
    _commitment(conn, item_id=bio, what="Complete the BIO 181 reading quiz",
                due_at="2026-09-14")

    assert _load(conn, settings, course="CHM 113").courses == ("BIO 181", "CHM 113")


def test_the_count_under_the_grid_counts_what_the_grid_shows(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A month narrowed to one course that went on reporting all of the month's
    assignments would be answering the question the owner just navigated away from."""
    for n, course in enumerate([
        "2026FallC-T-CHM113-LABORATORY", "2026FallC-T-BIO181-60069",
        "2026FallC-T-BIO181-60069",
    ]):
        item = _item(conn, source="canvas:ics", external=f"n{n}", title=f"Task {n}")
        _assignment(conn, external=f"n{n}", title=f"Task {n}", due_at="2026-09-14",
                    course=course, item_id=item)

    assert _load(conn, settings).counts == homework.Counts(
        assignments=3, linked=0, unlinked=3
    )
    assert _load(conn, settings, course="BIO 181").counts == homework.Counts(
        assignments=2, linked=0, unlinked=2
    )


def test_a_commitment_the_planner_placed_says_which_day(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The link the page exists to make. A deadline is a claim about when work is owed
    and a block is a claim about when it happens; only the second gets it done."""
    item = _item(conn, source="canvas:ics", external="p1", title="Lab report")
    _assignment(conn, external="p1", title="Lab report", due_at="2026-09-14", item_id=item)
    cid = _commitment(conn, item_id=item, what="Submit the lab report", due_at="2026-09-14")
    _plan(conn, day="2026-09-11", commitment_id=cid)

    entry = _cells(_load(conn, settings))[date(2026, 9, 14)].due[0]
    assert entry.placed is not None
    assert (entry.placed.day, entry.placed.label) == (date(2026, 9, 11), "11 Sep")


def test_a_superseded_plan_does_not_count_as_scheduled(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A replanned day leaves its old rows behind. Counting them reports work as
    scheduled on a day whose plan no longer exists — the same `status != 'superseded'`
    predicate `planner.current_plan_id` and the brief read a day's plan with."""
    item = _item(conn, source="canvas:ics", external="p2", title="Lab report")
    cid = _commitment(conn, item_id=item, what="Submit the CHM 113 lab report",
                      due_at="2026-09-14")
    _plan(conn, day="2026-09-11", commitment_id=cid, status="superseded")

    month = _load(conn, settings)
    assert _cells(month)[date(2026, 9, 14)].due[0].placed is None
    assert month.planned_through is None


def test_unplanned_stops_where_the_planner_has_reached(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The bound is the finding. The planner proposes one day at a time, so past its
    horizon "no block" means it has not looked yet — and a page that counted those would
    report the whole of next month as unscheduled every time it was opened.
    """
    near = _item(conn, source="canvas:ics", external="p3", title="Near")
    near_id = _commitment(conn, item_id=near, what="Do the near CHM 113 thing",
                          due_at="2026-09-08")
    far = _item(conn, source="canvas:ics", external="p4", title="Far")
    _commitment(conn, item_id=far, what="Do the far CHM 113 thing", due_at="2026-09-25")
    placed = _item(conn, source="canvas:ics", external="p5", title="Placed")
    placed_id = _commitment(conn, item_id=placed, what="Do the placed CHM 113 thing",
                            due_at="2026-09-09")
    _plan(conn, day="2026-09-10", commitment_id=placed_id)

    month = _load(conn, settings)
    assert month.planned_through == date(2026, 9, 10)
    assert month.planned_count == 1
    # The near one is inside the horizon and has no block. The far one is not a finding.
    assert [q.commitment_id for q in month.unplanned] == [near_id]


def test_the_month_is_reachable_from_the_pages_beside_it() -> None:
    """A page nothing points at is a page nobody opens. Day, week and Classes each name
    the month; the month names the day and the week back."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "backglass" / "web" / "templates"
    assert "/homework?month=" in (root / "schedule.html").read_text()
    assert "/homework?month=" in (root / "schedule_week.html").read_text()
    assert "/homework?course=" in (root / "classes.html").read_text()
    month = (root / "homework.html").read_text()
    assert "/schedule?date=" in month and "/schedule/week?start=" in month
    assert "/classes#panel-course-" in month


def test_the_filters_survive_the_pager(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A month walked while filtered that silently dropped the filter on the next arrow
    would change the question under the owner."""
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    body = client.get("/homework?month=2026-09&course=CHM+113&only=coursework").text
    assert "/homework?month=2026-10&amp;only=coursework&amp;course=CHM+113" in body
    assert "/homework?month=2026-08&amp;only=coursework&amp;course=CHM+113" in body
    # And the link is followed, not only read. `keep` is a variable, so a `&amp;` written
    # into it is escaped twice, the href reads `&amp;only=`, and the browser sends a
    # parameter literally named `amp;only` — an href that looks right in the source and
    # drops the filter on the first click.
    followed = client.get("/homework?month=2026-10&only=coursework&course=CHM+113")
    assert followed.status_code == 200
    assert "October 2026" in followed.text
    assert "show everything due" in followed.text  # the coursework filter survived
    assert "CHM 113 on Classes" in followed.text  # and so did the course


def test_the_route_refuses_what_is_not_a_course_code(conn, settings) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    assert client.get("/homework?course=CHM+113").status_code == 200
    assert client.get("/homework?course=%27+OR+1%3D1").status_code == 422
    assert client.get("/homework?course=").status_code == 422


def test_an_empty_filtered_month_says_which_kind_of_empty_it_is(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """docs/06 §Empty states. A course filter on a month holding none of it draws
    forty-two empty framed cells and three zeroes, which reads as broken rather than as
    nothing due — and the way out of the filter has to be in the sentence."""
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    body = client.get("/homework?month=2026-09&course=LSB+191").text
    assert "Nothing due and nothing on the calendar for LSB 191" in body
    assert "Every course" in body
    for banned in ("caught up", "🎉", "Great job", "Nice work"):
        assert banned not in body


def test_a_filtered_page_says_so_on_both_of_its_counts(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Two panels that both say "this month" while one is filtered and the other is not
    is a page disagreeing with itself about what it is showing."""
    item = _item(conn, source="canvas:ics", external="f1", title="Lab report")
    _assignment(conn, external="f1", title="Lab report", due_at="2026-09-14", item_id=item)
    _commitment(conn, item_id=item, what="Submit the lab report", due_at="2026-09-14")

    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    body = client.get("/homework?month=2026-09&course=CHM+113").text
    assert body.count("in CHM 113") >= 2


# ── the to-do list ────────────────────────────────────────────────────────────────
#
# The Homework tab's default register (owner, 2026-08-29). The month grid is a shape and
# this is an order, and the order is the thing worth testing: a list nobody can predict
# the top of is a list they re-sort in their head.


def _todo(conn: sqlite3.Connection, settings: Settings, **kwargs: object) -> homework.Todo:
    return homework.todo(
        conn, settings, today=TODAY, now=NOW, **kwargs  # type: ignore[arg-type]
    )


def _titles(view: homework.Todo) -> list[str]:
    return [item.title for bucket in view.buckets for item in bucket.items]


def test_the_list_is_ranked_by_day_then_hour_then_longest(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Owner, 2026-08-29: "ranks all homework by due date and time it takes". The
    estimate is the tiebreaker and not the spine — two things due Friday are not the same
    problem when one is four hours and the other is ten minutes."""
    item = _item(conn, source="gmail", external="r1", title="mail")
    _commitment(conn, item_id=item, what="friday short", due_at="2026-09-11", minutes=10)
    _commitment(conn, item_id=item, what="friday long", due_at="2026-09-11", minutes=240)
    _commitment(
        conn, item_id=item, what="friday at nine", due_at="2026-09-11T09:00:00", minutes=5
    )
    _commitment(conn, item_id=item, what="thursday", due_at="2026-09-10", minutes=5)

    assert _titles(_todo(conn, settings)) == [
        "thursday",
        # A stated hour outranks a bare date on the same day: an 11:59pm deadline is a
        # deadline and "sometime Friday" is not.
        "friday at nine",
        "friday long",
        "friday short",
    ]


def test_overdue_runs_most_recently_missed_first(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Sorted the obvious way, the top of the live list was a hall parking permit due
    six weeks earlier, printed under the words "start here". Staleness is not urgency:
    yesterday's miss is usually recoverable and July's is a ledger hygiene problem."""
    item = _item(conn, source="gmail", external="r2", title="mail")
    _commitment(conn, item_id=item, what="ancient", due_at="2026-07-06", minutes=15)
    _commitment(conn, item_id=item, what="yesterday", due_at="2026-09-09", minutes=15)
    _commitment(conn, item_id=item, what="last week", due_at="2026-09-02", minutes=15)

    view = _todo(conn, settings)
    assert [q.title for q in view.buckets[0].items] == ["yesterday", "last week", "ancient"]
    assert view.buckets[0].key == "overdue"
    assert view.next_up is not None and view.next_up.title == "yesterday"


def test_the_bands_are_named_and_carry_their_own_load(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The number the owner acts on is hours, not items."""
    item = _item(conn, source="gmail", external="r3", title="mail")
    _commitment(conn, item_id=item, what="today a", due_at="2026-09-10", minutes=45)
    _commitment(conn, item_id=item, what="today b", due_at="2026-09-10", minutes=90)
    _commitment(conn, item_id=item, what="tomorrow", due_at="2026-09-11", minutes=30)
    _commitment(conn, item_id=item, what="december", due_at="2026-12-01", minutes=60)

    view = _todo(conn, settings)
    bands = {bucket.key: bucket for bucket in view.buckets}
    assert set(bands) == {"today", "tomorrow", "later"}
    assert bands["today"].minutes == 135
    assert bands["today"].load_label == "2h15"
    assert bands["later"].label == "Later"
    assert view.count == 4


def test_an_unestimated_row_adds_nothing_and_is_counted_saying_so(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A band reading 45m that is really 45m plus nine unsized items is a number
    describing where the counting stopped, read as a description of the work."""
    item = _item(conn, source="gmail", external="r4", title="mail")
    _commitment(conn, item_id=item, what="sized", due_at="2026-09-10", minutes=45)
    _commitment(conn, item_id=item, what="unsized", due_at="2026-09-10", minutes=None)

    view = _todo(conn, settings)
    assert view.minutes == 45
    assert view.unestimated == 1
    assert view.buckets[0].unestimated == 1


def test_finished_work_is_not_on_the_list(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The grid keeps it — a month read back with its finished work erased reads as a
    month where nothing happened. A list is read forward."""
    item = _item(conn, source="gmail", external="r5", title="mail")
    _commitment(conn, item_id=item, what="done one", due_at="2026-09-11", status="done")
    _commitment(conn, item_id=item, what="open one", due_at="2026-09-11")
    assert _titles(_todo(conn, settings)) == ["open one"]


def test_an_assignment_with_no_commitment_is_still_listed(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The 2026-08-27 failure, one surface along: twenty-two pieces of graded work were
    invisible everywhere because every surface listed only what another had extracted."""
    item = _item(conn, source="canvas:ics", external="u1", title="Unlinked lab")
    _assignment(conn, external="u1", title="Unlinked lab", due_at="2026-09-14", item_id=item)
    view = _todo(conn, settings)
    assert _titles(view) == ["Unlinked lab"]
    assert view.buckets[0].items[0].kind == "unlinked"


def test_the_list_says_what_its_filter_is_keeping_off_it(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A surface that narrows quietly is a surface whose count the owner reads as the
    whole ledger."""
    item = _item(conn, source="gmail", external="r6", title="mail")
    _commitment(conn, item_id=item, what="renew the parking permit", due_at="2026-09-11")
    _commitment(conn, item_id=item, what="CHM 113 safety quiz writeup", due_at="2026-09-11")
    canvas = _item(conn, source="canvas:ics", external="c9", title="Lab 2")
    _assignment(conn, external="c9", title="Lab 2", due_at="2026-09-12", item_id=canvas)

    everything = _todo(conn, settings)
    assert everything.filtered_out == 0
    assert len(_titles(everything)) == 3

    coursework_only = _todo(conn, settings, only_coursework=True)
    assert "renew the parking permit" not in _titles(coursework_only)
    assert coursework_only.filtered_out == 1


def test_a_title_does_not_print_the_course_it_is_already_labelled_with(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """`CIS 236 · CIS 236: watch 1-3-2` clipped in the grid to `CIS 236 CIS 236: watch
    1-…`, spending the width on the half that identified nothing."""
    item = _item(conn, source="canvas:ics", external="s1", title="t")
    _assignment(
        conn,
        external="s1",
        title="CHM 113: Post-Lab writeup",
        due_at="2026-09-14",
        item_id=item,
    )
    row = _todo(conn, settings).buckets[0].items[0]
    assert row.course.startswith("CHM 113")
    assert row.short_title == "Post-Lab writeup"


def test_an_isbn_is_not_a_course(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """`_CODE` is "two to four letters then three digits", which is what an ASU course
    code is made of and also what an ISBN is made of. `Buy Norton — ISBN 978-0-393…`
    reached the live chip row as a class the owner could filter to."""
    item = _item(conn, source="gmail", external="i1", title="mail")
    _commitment(
        conn, item_id=item, what="Buy Norton reader ISBN 978-0-393-42827-4", due_at="2026-09-14"
    )
    # A real course code in a sentence still resolves: the guard is on the fallback only,
    # and the enrolled set comes from what Canvas has actually issued.
    _assignment(conn, external="e1", title="Lab 1", due_at="2026-09-14")
    _commitment(conn, item_id=item, what="finish the CHM 113 safety quiz", due_at="2026-09-15")

    view = _todo(conn, settings)
    assert "ISBN 978" not in view.courses
    assert "CHM 113" in view.courses


def test_the_row_links_to_the_assignment_and_carries_its_walkthrough(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Owner, 2026-08-29: "each thing should have a hyperlink to the exact assignment
    with a walkthrough". The feed's own URL goes to the calendar."""
    item = _item(conn, source="canvas:ics", external="w1", title="Dataset Evaluation")
    conn.execute(
        "INSERT INTO assignment (user_id, source, external_id, source_item_id, course,"
        " title, due_at, url, description, description_hash, effort_minutes,"
        " points_possible, first_seen_at, last_changed_at)"
        " VALUES (?, 'canvas:ics', 'assignment:7833125', ?, ?, ?, ?, ?, ?, 'h', 172, 60.0,"
        " ?, ?)",
        (
            USER_ID,
            item,
            "2026FallC-T-CIS236-87708",
            "T - Dataset Evaluation",
            "2026-09-14",
            "https://canvas.asu.edu/calendar?include_contexts=course_274090&month=09"
            "&year=2026#assignment_7833125",
            "1. Download the provided dataset.\n2. Build the chart in Excel.\n",
            now_iso(),
            now_iso(),
        ),
    )
    conn.commit()

    row = _todo(conn, settings).buckets[0].items[0]
    assert row.href == "https://canvas.asu.edu/courses/274090/assignments/7833125"
    assert row.walk is not None and row.walk.offered
    assert "Download the provided dataset." in [step.text for step in row.walk.steps]


def test_the_tab_opens_on_the_list_and_the_month_keeps_its_own_url(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Every link `/schedule`, `/schedule/week` and `/classes` already carry names a
    month, and naming a month is asking for the month."""
    from tests.conftest import panel_slice

    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    listed = client.get("/homework")
    assert listed.status_code == 200
    assert "panel-homework-total" in listed.text or "panel-homework-empty" in listed.text
    assert "panel-homework-grid" not in listed.text

    grid = client.get("/homework?month=2026-09")
    assert grid.status_code == 200
    assert panel_slice(grid.text, "panel-homework-grid")

    assert "panel-homework-grid" in client.get("/homework?view=month").text
    assert "panel-homework-grid" not in client.get("/homework?month=2026-09&view=list").text


def test_the_month_no_longer_draws_a_chip_with_no_name(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """`chips` appended the current filter whenever it was not already in the list, and
    an unfiltered page has `course == ""` — so an empty chip rendered beside `all`, both
    marked `on`, because `"" == ""`."""
    item = _item(conn, source="canvas:ics", external="ch1", title="Lab 3")
    _assignment(conn, external="ch1", title="Lab 3", due_at="2026-09-14", item_id=item)

    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    body = client.get("/homework?month=2026-09").text
    assert 'course="\n' not in body
    assert "?month=2026-09&amp;course=\"" not in body
    assert body.count('class="b2 on"') == 1  # `all`, and nothing else


def test_the_month_folds_its_calendar_instead_of_being_it(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Measured 2026-08-29 on the live ledger: 159 event tiles against 160 due tiles,
    events uncapped while the deadlines folded at six. Owner's ruling the same day: fold
    them, do not drop them — `capacity.calendar_events` stays the reader, so this grid
    and `/schedule` cannot disagree about what a Tuesday holds."""
    for n in range(4):
        _calendar(
            conn,
            source="calendar:asu",
            external=f"ev{n}",
            title=f"CHM 113 (Lab) meeting {n}",
            starts_at=f"2026-09-15T{8 + n:02d}:00:00-07:00",
            ends_at=f"2026-09-15T{9 + n:02d}:00:00-07:00",
        )
    _calendar(
        conn,
        source="calendar:asu",
        external="ev9",
        title="Advising with Rachel",
        starts_at="2026-09-15T15:00:00-07:00",
        ends_at="2026-09-15T15:30:00-07:00",
    )

    cell = _cells(_load(conn, settings))[date(2026, 9, 15)]
    assert len(cell.events) == 5
    assert cell.event_summary == "4 classes · 1 event"

    from tests.conftest import panel_slice

    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    grid = panel_slice(client.get("/homework?month=2026-09").text, "panel-homework-grid")
    # Every event is still in the page, and none of them is a tile until it is opened.
    assert "4 classes · 1 event" in grid
    assert "Advising with Rachel" in grid
    assert grid.count("hwmore hwev") == 1
