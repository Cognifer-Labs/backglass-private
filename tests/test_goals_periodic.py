"""Periodic targets — obligations that repeat on a clock nobody sends mail about.

The motivating instance is the one the ledger structurally cannot see: academic
advising every six months, an internship cycle that reopens every year, a research
placement to go looking for. No Canvas assignment carries them and no inbox generates
them, so unlike a deadline in an email they are reached by a clock or by nobody.

Migration 0024. Due at the cadence, overdue at twice it, reset by a checkpoint.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.goals import checkpoints
from backglass.goals import targets as targets_mod
from backglass.roadmap import instantiate
from backglass.web.app import create_app

TODAY = date(2026, 8, 14)


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _goal(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, target_date, definition_of_done,"
        " status, created_at) VALUES (1, 'Get into a competitive med school', 'annual',"
        " '2029-06-15', 'Accepted', 'active', '2026-07-01T00:00:00Z')"
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _periodic(
    conn: sqlite3.Connection,
    goal_id: int,
    *,
    every_days: int = 182,
    days_ago: int = 0,
    today: date = TODAY,
) -> int:
    """A periodic target whose clock was last reset `days_ago`, via created_at.

    `today` exists for the page tests: the route reads the real clock, and anchoring
    those against the frozen TODAY would pass on 2026-08-14 and fail on the 15th.
    """
    anchor = (today - timedelta(days=days_ago)).isoformat() + "T12:00:00"
    return instantiate.add_periodic(
        conn, goal_id, "Academic advising check-in", every_days, created_at=anchor
    )


def _now(settings: Settings) -> date:
    """The date the route itself will use, so a page test survives tomorrow."""
    from backglass.plan import timezones

    return timezones.today_for(settings)


def _only(conn: sqlite3.Connection, settings: Settings) -> targets_mod.TargetProgress:
    rows = [
        p
        for p in targets_mod.progress(conn, settings, TODAY)
        if p.kind == "periodic"
    ]
    assert len(rows) == 1
    return rows[0]


class TestSchema:
    def test_every_days_column_exists_and_migration_is_idempotent(
        self, conn: sqlite3.Connection
    ) -> None:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(target)")}
        assert "every_days" in cols
        from backglass.db import migrate

        migrate(conn)
        assert "every_days" in {r["name"] for r in conn.execute("PRAGMA table_info(target)")}


class TestClock:
    def test_a_new_target_is_not_instantly_overdue(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The whole reason `created_at` is the fallback anchor. Without it, every
        periodic target renders overdue the second it is written — a wall of alarm
        ink over nothing having gone wrong yet."""
        _periodic(conn, _goal(conn), days_ago=0)
        target = _only(conn, settings)
        assert target.days_since == 0
        assert target.level == "fresh"
        assert target.complete

    def test_fresh_until_the_cadence(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _periodic(conn, _goal(conn), every_days=182, days_ago=181)
        assert _only(conn, settings).level == "fresh"

    def test_due_at_the_cadence(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _periodic(conn, _goal(conn), every_days=182, days_ago=182)
        target = _only(conn, settings)
        assert target.level == "due"
        assert not target.complete
        assert target.chip() == "182 days since last"

    def test_overdue_at_twice_the_cadence(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _periodic(conn, _goal(conn), every_days=182, days_ago=364)
        assert _only(conn, settings).level == "overdue"

    def test_a_checkpoint_resets_the_clock(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        target_id = _periodic(conn, _goal(conn), every_days=182, days_ago=400)
        assert _only(conn, settings).level == "overdue"
        checkpoints.record(
            conn,
            target_id,
            source="manual",
            occurred_at=(TODAY - timedelta(days=3)).isoformat() + "T12:00:00",
        )
        target = _only(conn, settings)
        assert target.days_since == 3
        assert target.level == "fresh"

    def test_deleting_the_checkpoint_puts_it_straight_back(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """G10 for free: nothing stores a due date, so a deleted reset is undone on
        the next read rather than leaving a stale next-due column behind."""
        target_id = _periodic(conn, _goal(conn), every_days=182, days_ago=400)
        recorded = checkpoints.record(conn, target_id, source="manual")
        assert _only(conn, settings).level == "fresh"
        checkpoints.delete(conn, recorded.checkpoint_id)
        assert _only(conn, settings).level == "overdue"

    def test_the_day_count_is_local_not_the_utc_prefix(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The owner moves between UTC-7 and UTC+5:30. A checkpoint written at 22:00
        in Phoenix is stored as the *next* UTC day, and slicing `[:10]` would report
        the gap one day short — `count_between` documents the same bug for weeks."""
        target_id = _periodic(conn, _goal(conn), every_days=182, days_ago=400)
        # One instant, two calendars: 2026-08-09T05:00 UTC is the evening of the 8th in
        # Phoenix (UTC-7) and the morning of the 9th in Kolkata (UTC+5:30).
        checkpoints.record(
            conn, target_id, source="manual", occurred_at="2026-08-09T05:00:00"
        )
        assert settings.default_tz == "America/Phoenix"
        assert _only(conn, settings).days_since == 6  # the 8th, not the 9th
        kolkata = settings.model_copy(update={"default_tz": "Asia/Kolkata"})
        assert _only(conn, kolkata).days_since == 5

    def test_a_future_anchor_is_zero_never_negative(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _periodic(conn, _goal(conn), days_ago=-30)
        assert _only(conn, settings).days_since == 0


class TestItStaysOutOfTheWeeklyMachinery:
    def test_contributes_no_weekly_minutes(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A six-month obligation is not weekly load. Amortising it would add two
        minutes a week to the §2.3 capacity check and make G6's gap unactionable."""
        _periodic(conn, _goal(conn), days_ago=400)
        assert _only(conn, settings).weekly_minutes == 0

    def test_is_never_flagged_unrealistic(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """G4 counts consecutive missed *weeks*. A target that was never owed in any
        of them cannot have missed them."""
        _periodic(conn, _goal(conn), days_ago=400)
        target = _only(conn, settings)
        assert target.missed_weeks == 0
        assert not target.unrealistic(settings)


class TestDefaultTarget:
    def test_a_resolved_commitment_never_resets_a_periodic_clock(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The trap this feature could most easily have shipped with.

        `_default_target` falls back to the oldest active target of any kind, so a
        goal whose only target is periodic would have had "see your advisor" marked
        done every time any goal-linked commitment closed — a valid id, a real row,
        no error, and the wrong table's meaning. The 2026-08-12 failure mode.
        """
        goal_id = _goal(conn)
        _periodic(conn, goal_id, every_days=182, days_ago=400)
        assert checkpoints._default_target(conn, goal_id) is None
        assert _only(conn, settings).level == "overdue"

    def test_a_cadence_target_is_still_the_default(
        self, conn: sqlite3.Connection
    ) -> None:
        """Excluding periodic must not change the answer for goals that have both."""
        goal_id = _goal(conn)
        _periodic(conn, goal_id)
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, active, created_at)"
            " VALUES (?, 'cadence', 'MCAT practice sections', 5, 1, '2026-07-01T00:00:00Z')",
            (goal_id,),
        )
        cadence_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        assert checkpoints._default_target(conn, goal_id) == cadence_id


class TestGoalsPage:
    def test_the_card_carries_the_day_count_and_the_chip(
        self, conn: sqlite3.Connection, settings: Settings, client: TestClient
    ) -> None:
        """The dashboard must agree with the brief, and both read one rule. A due date
        computed twice is a due date that disagrees with itself."""
        _periodic(conn, _goal(conn), every_days=182, days_ago=400, today=_now(settings))
        page = client.get("/goals").text
        assert "Academic advising check-in" in page
        assert "400 days since last, every 182 days" in page
        assert "Overdue" in page

    def test_a_fresh_one_renders_without_an_alarm(
        self, conn: sqlite3.Connection, settings: Settings, client: TestClient
    ) -> None:
        _periodic(conn, _goal(conn), every_days=182, days_ago=10, today=_now(settings))
        page = client.get("/goals").text
        assert "10 days since last, every 182 days" in page
        assert "Overdue" not in page

    def test_a_due_one_does_not_claim_to_be_due_today(
        self, conn: sqlite3.Connection, settings: Settings, client: TestClient
    ) -> None:
        """`due` spans everything from the cadence to twice it — for a semiannual
        target that is half a year. Borrowing the `due_today` label would print a
        false claim on every one of those days."""
        _periodic(conn, _goal(conn), every_days=182, days_ago=200, today=_now(settings))
        page = client.get("/goals").text
        assert "200 days since last" in page
        assert "Due today" not in page
        assert ">Due<" in page

    def test_it_is_not_collapsed_into_the_quiet_milestone_count(
        self, conn: sqlite3.Connection, settings: Settings, client: TestClient
    ) -> None:
        """A never-done periodic target matches the "not started" shape exactly — no
        weekly_count, no checkpoint — while being the one kind whose whole point is
        that the clock ran without it. Collapsing it hides an overdue advising
        check-in behind the words "N milestones not started"."""
        from backglass.web import panels

        _periodic(conn, _goal(conn), every_days=182, days_ago=400)
        panel = panels.goals_panel(conn, settings, TODAY)
        assert panel.meta["quiet_milestones"] == 0
        assert [r for r in panel.rows if r["kind"] == "periodic"]


class TestBrief:
    def test_the_daily_brief_says_so_when_it_is_due(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.brief import daily

        _periodic(conn, _goal(conn), every_days=182, days_ago=400)
        section = daily.goal_section(conn, TODAY, settings)
        lines = [line for line in section.lines if "advising" in line.text]
        assert len(lines) == 1
        # G12: a day count, never a bare colour.
        assert "400 days since last" in lines[0].text
        assert "every 182 days" in lines[0].text
        assert lines[0].status == "overdue"
        # CLAUDE.md rule 1: the claim points at the goal it came from.
        assert lines[0].provenance is not None

    def test_the_daily_brief_is_silent_while_it_is_fresh(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """B3: a quiet day says nothing. 175 days of silence is the feature."""
        from backglass.brief import daily

        _periodic(conn, _goal(conn), every_days=182, days_ago=100)
        section = daily.goal_section(conn, TODAY, settings)
        assert not [line for line in section.lines if "advising" in line.text]

    def test_the_monday_brief_does_not_score_it_as_a_week(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """"0 missed" fifty-one weeks out of fifty-two is the noise the total and
        milestone branches were already written to avoid."""
        from backglass.brief import weekly

        _periodic(conn, _goal(conn), every_days=182, days_ago=400)
        brief = weekly.monday(conn, settings, TODAY)
        text = "\n".join(
            line.text for section in brief.sections for line in section.lines
        )
        assert "advising" not in text
