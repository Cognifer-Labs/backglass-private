"""Routines and the 12-hour clock: life on the schedule, times a person reads."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backglass.config import RoutineError, Settings, parse_routines
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan import capacity, planner, timezones
from backglass.web.app import create_app

THURSDAY = date(2026, 7, 30)  # a working day in the fixture settings
PHOENIX = "America/Phoenix"


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


class TestParser:
    def test_parses_and_sorts(self) -> None:
        routines = parse_routines("gym@17:30+60, breakfast@07:30+30")
        assert [(r.name, r.start_minute, r.minutes) for r in routines] == [
            ("breakfast", 450, 30),
            ("gym", 1050, 60),
        ]

    def test_blank_means_none(self) -> None:
        assert parse_routines("") == []
        assert parse_routines(" , ") == []

    @pytest.mark.parametrize(
        "raw",
        ["lunch", "lunch@noon+30", "lunch@12:30", "lunch@25:00+30", "lunch@12:75+30",
         "lunch@12:30+0", "lunch@12:30+2000"],
    )
    def test_every_malformed_shape_leaves_by_one_door(self, raw: str) -> None:
        with pytest.raises(RoutineError):
            parse_routines(raw)


class TestClock12:
    @pytest.mark.parametrize(
        ("minute", "label"),
        [(0, "12:00am"), (450, "7:30am"), (719, "11:59am"), (720, "12:00pm"),
         (750, "12:30pm"), (1140, "7:00pm"), (1439, "11:59pm")],
    )
    def test_boundaries(self, minute: int, label: str) -> None:
        assert timezones.clock12(minute) == label

    def test_hour_ruler_labels(self) -> None:
        assert [timezones.hour12(h) for h in (0, 8, 12, 19, 24)] == [
            "12am", "8am", "12pm", "7pm", "12am"
        ]

    def test_t12_reads_iso_and_bare_times(self) -> None:
        assert timezones.t12("2026-07-30T19:15:00-07:00") == "7:15pm"
        assert timezones.t12("09:05") == "9:05am"


class TestCapacity:
    def test_only_the_in_window_slice_of_a_routine_spends_capacity(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Lunch (45m, inside 09:00–18:00) and the first half hour of gym spend
        capacity; breakfast, shower and dinner are life outside the window and
        spend nothing."""
        without = capacity.compute(
            conn, settings.model_copy(update={"routines": ""}), THURSDAY
        )
        with_routines = capacity.compute(conn, settings, THURSDAY)
        assert without.capacity_minutes - with_routines.capacity_minutes == 45 + 30
        # No meeting buffer for a meal: the difference is exactly the clipped spans.
        assert with_routines.buffer_minutes == without.buffer_minutes

    def test_day_events_carries_the_whole_day(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        titles = [e.title for e in capacity.day_events(conn, settings, THURSDAY)]
        assert titles == ["Breakfast", "Lunch", "Gym", "Shower", "Dinner"]
        assert all(
            e.kind == "routine" for e in capacity.day_events(conn, settings, THURSDAY)
        )


class TestPlanner:
    def test_the_plan_holds_the_whole_day_not_the_shift(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        proposal = planner.propose(conn, settings, THURSDAY)
        routine = [b for b in proposal.blocks if b["kind"] == "routine"]
        assert [b["title"] for b in routine] == [
            "Breakfast", "Lunch", "Gym", "Shower", "Dinner"
        ]
        # Out-of-window routines are on the plan even though they spend no capacity.
        assert any(str(b["starts_at"])[11:16] == "07:30" for b in routine)
        assert any(str(b["starts_at"])[11:16] == "19:15" for b in routine)
        # And planned work never lands inside one that is in the window.
        lunch = next(b for b in routine if b["title"] == "Lunch")
        for block in proposal.blocks:
            if block["kind"] in ("work", "protected", "small"):
                assert not (
                    block["starts_at"] < lunch["ends_at"]
                    and block["ends_at"] > lunch["starts_at"]
                ), block


def an_evening_plan(
    conn: sqlite3.Connection, day: date, what: str = "Dinner with Priya"
) -> None:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'imessage', ?, ?, '2026-07-28T09:15:00-07:00',"
        " 'Priya', 'Dinner', 'b', '{}', ?, 'keep')",
        (USER_ID, f"m-{what}", now_iso(), f"h-{what}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
        " when_is_explicit, status, confidence, source_item_id, created_at)"
        " VALUES (?, 'social', ?, ?, ?, 1, 'confirmed', 0.95, ?, ?)",
        (
            USER_ID,
            what,
            f"{day.isoformat()}T19:30:00-07:00",
            f"{day.isoformat()}T21:00:00-07:00",
            source_id,
            now_iso(),
        ),
    )


class TestSchedulePage:
    def test_the_day_shows_life_in_a_clock_a_person_reads(
        self, client: TestClient
    ) -> None:
        page = client.get(f"/schedule?date={THURSDAY.isoformat()}").text
        # Routines render without any planner having visited the day...
        for title in ("Breakfast", "Lunch", "Gym", "Shower", "Dinner"):
            assert title in page, title
        assert "k-routine" in page
        # ...the ruler reaches the morning and the evening...
        assert "7am" in page
        assert "8pm" in page
        # ...and every time on the page is am/pm, none is army time.
        assert "7:30am–8:00am" in page
        assert "7:15pm–8:00pm" in page
        assert "07:30" not in page
        # The planner hint survives the day no longer being empty.
        assert "backglass plan" in page

    def test_an_evening_plan_is_finally_on_the_schedule(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """The gap tasks/todo.md documented as a design call: a confirmed 7:30pm
        dinner was stored, spent no capacity, and appeared on no schedule surface.
        The day is the page now, so it renders."""
        an_evening_plan(conn, THURSDAY)
        conn.commit()
        page = client.get(f"/schedule?date={THURSDAY.isoformat()}").text
        assert "Dinner with Priya" in page
        assert "7:30pm–9:00pm" in page

    def test_week_grid_needs_more_than_the_routine_template(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        empty = client.get("/schedule/week?start=2026-07-27").text
        assert 'class="wk7"' not in empty  # routines alone are not a week of plans
        an_evening_plan(conn, THURSDAY)
        conn.commit()
        busy = client.get("/schedule/week?start=2026-07-27").text
        assert 'class="wk7"' in busy
        assert "Breakfast" in busy  # once real, the grid carries the routines too
