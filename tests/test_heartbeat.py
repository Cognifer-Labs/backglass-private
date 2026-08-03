"""Silent-failure visibility. docs/11 §8, CLAUDE.md rule 5.

"The dangerous failure is not the error, it is a brief that looks complete and is not."
A launchd job that stops firing is exactly that failure: nothing errors, every panel
reads green, and the brief is confidently reporting a ledger that stopped moving on
Tuesday. These tests are the only thing standing between that and a silent product.

Every case injects `now`. A staleness test that reads the wall clock passes at 09:00 and
fails at 23:00, which is worse than no test.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backglass import heartbeat
from backglass.brief import daily
from backglass.config import Settings
from backglass.web import panels
from backglass.web.app import create_app
from tests.conftest import healthy_run

#: A Thursday. Working day in the default settings, so the plan window applies.
TODAY = date(2026, 7, 30)
#: 10:00 in Phoenix (UTC-7), well past the 05:45 + 30m plan window.
NOW = datetime(2026, 7, 30, 17, 0, tzinfo=UTC)


def a_run(conn: sqlite3.Connection, ago: timedelta) -> int:
    return healthy_run(conn, (NOW - ago).isoformat())


def a_plan(conn: sqlite3.Connection, day: date = TODAY, status: str = "proposed") -> None:
    """A plan with one work block — the block matters because a plan with none is
    "fully booked", which failure_section already reports for its own reasons."""
    conn.execute(
        "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes, generated_at, "
        " status) VALUES (1, ?, 'America/Phoenix', 240, ?, ?)",
        (day.isoformat(), NOW.isoformat(), status),
    )
    plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title) "
        "VALUES (?, ?, ?, 'work', 'draft the migration plan')",
        (plan_id, f"{day}T09:00:00-07:00", f"{day}T10:30:00-07:00"),
    )


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn  # migrated db on disk; the app opens its own connections
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


# ── the reading itself ────────────────────────────────────────────────────


class TestStaleness:
    def test_a_fresh_run_is_not_stale(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_run(conn, timedelta(minutes=20))
        beat = heartbeat.read(conn, settings, TODAY, NOW)
        assert not beat.stale
        assert not beat.never_ran
        assert beat.age_hours == pytest.approx(20 / 60, abs=0.01)

    def test_four_missed_cycles_is_stale(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Default threshold is 2h — four times the 30-minute sync cadence."""
        a_run(conn, timedelta(hours=26))
        beat = heartbeat.read(conn, settings, TODAY, NOW)
        assert beat.stale
        assert beat.age_phrase == "26h ago"

    def test_the_threshold_is_a_setting(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_run(conn, timedelta(hours=3))
        assert heartbeat.read(conn, settings, TODAY, NOW).stale
        relaxed = settings.model_copy(update={"sync_stale_after_hours": 8.0})
        assert not heartbeat.read(conn, relaxed, TODAY, NOW).stale

    def test_never_run_is_its_own_state(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """"0h ago" and "never" are different problems with different fixes, so
        never-ran is reported separately rather than as an infinitely stale run."""
        beat = heartbeat.read(conn, settings, TODAY, NOW)
        assert beat.never_ran
        assert not beat.stale
        assert beat.last_run_at is None

    def test_a_started_but_unfinished_run_does_not_count(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A sync that began and never came back is not evidence the ledger is current."""
        conn.execute(
            "INSERT INTO run (user_id, started_at, finished_at) VALUES (1, ?, NULL)",
            ((NOW - timedelta(minutes=5)).isoformat(),),
        )
        assert heartbeat.read(conn, settings, TODAY, NOW).never_ran

    def test_the_newest_completed_run_wins_whatever_the_insert_order(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        newest = a_run(conn, timedelta(minutes=10))
        a_run(conn, timedelta(days=4))
        assert heartbeat.read(conn, settings, TODAY, NOW).last_run_id == newest


class TestFailedSources:
    def test_failed_sources_are_named_with_their_age(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.connectors import credentials

        a_run(conn, timedelta(hours=26))
        credentials.mark_failed(conn, "gmail:personal", "invalid_grant")
        conn.execute(
            "UPDATE credential SET updated_at = ? WHERE source = 'gmail:personal'",
            ((NOW - timedelta(days=3)).isoformat(),),
        )
        beat = heartbeat.read(conn, settings, TODAY, NOW)
        assert [s.source for s in beat.failed_sources] == ["gmail:personal"]
        assert beat.failed_phrase == "gmail:personal failing since 3d ago"

    def test_a_paused_source_is_not_a_failing_one(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The owner chose the silence, so it is not a failure state."""
        from backglass.connectors import credentials

        credentials.mark_failed(conn, "gmail:personal", "invalid_grant")
        credentials.set_enabled(conn, "gmail:personal", False)
        assert heartbeat.read(conn, settings, TODAY, NOW).failed_sources == []
        assert heartbeat.read(conn, settings, TODAY, NOW).failed_phrase is None


class TestPlanWindow:
    def test_before_the_window_a_missing_plan_is_not_an_alert(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        early = datetime(2026, 7, 30, 12, 30, tzinfo=UTC)  # 05:30 in Phoenix
        assert not heartbeat.read(conn, settings, TODAY, early).plan_due

    def test_after_the_window_and_grace_a_missing_plan_is_an_alert(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        late = datetime(2026, 7, 30, 13, 20, tzinfo=UTC)  # 06:20 in Phoenix
        beat = heartbeat.read(conn, settings, TODAY, late)
        assert beat.plan_due
        assert beat.plan_missing

    def test_a_live_plan_silences_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_plan(conn)
        beat = heartbeat.read(conn, settings, TODAY, NOW)
        assert beat.plan_id is not None
        assert not beat.plan_missing

    def test_a_superseded_plan_does_not_count_as_a_plan(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_plan(conn, status="superseded")
        assert heartbeat.read(conn, settings, TODAY, NOW).plan_missing

    def test_a_non_working_day_is_never_missing_a_plan(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """docs/04 P3: a weekend has no capacity and so no plan. Alerting every
        Saturday is how an alert block gets ignored on the Tuesday it matters."""
        saturday = date(2026, 8, 1)
        moment = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
        assert not heartbeat.read(conn, settings, saturday, moment).plan_due

    def test_a_past_day_is_history_not_an_alert(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A brief regenerated for last Tuesday must not claim the planner is broken."""
        assert not heartbeat.read(conn, settings, date(2026, 7, 28), NOW).plan_due


class TestBothTimezoneDirections:
    """The owner moves between UTC-7 and UTC+5:30, and the plan window is a *local*
    05:45. One instant is therefore two different answers, which is the whole trap."""

    def test_one_instant_is_before_the_window_west_and_after_it_east(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        moment = datetime(2026, 7, 30, 12, 40, tzinfo=UTC)  # 05:40 Phoenix / 18:10 Kolkata
        east = settings.model_copy(update={"default_tz": "Asia/Kolkata"})
        assert not heartbeat.read(conn, settings, TODAY, moment).plan_due
        assert heartbeat.read(conn, east, TODAY, moment).plan_due

    def test_the_local_day_boundary_decides_which_day_is_today(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """02:00Z on the 31st is still the evening of the 30th in Phoenix and already
        the morning of the 31st in Kolkata. Compared as instants, never as `date()`
        over an offset string — see plan/timezones.utc_bounds."""
        friday = date(2026, 7, 31)
        moment = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
        east = settings.model_copy(update={"default_tz": "Asia/Kolkata"})
        assert not heartbeat.read(conn, settings, friday, moment).plan_due
        assert heartbeat.read(conn, east, friday, moment).plan_due

    def test_a_tz_range_moves_the_window_with_the_owner(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """P14: the active zone comes from the explicit range, not from a fixed offset."""
        travelling = settings.model_copy(update={"tz_ranges": ["2026-07-29:Asia/Kolkata"]})
        moment = datetime(2026, 7, 30, 12, 40, tzinfo=UTC)
        beat = heartbeat.read(conn, travelling, TODAY, moment)
        assert beat.tz == "Asia/Kolkata"
        assert beat.plan_due


# ── the dashboard sidebar ─────────────────────────────────────────────────


class TestSidebarAlerts:
    def test_a_stale_sync_is_a_vermilion_alert_naming_the_age(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_run(conn, timedelta(hours=26))
        a_plan(conn)
        alerts = panels.sidebar(conn, settings, TODAY, NOW).alerts
        assert alerts[0]["level"] == "verm"
        assert alerts[0]["text"] == "Last sync ran 26h ago — scheduled jobs may be dead"
        assert alerts[0]["href"] == "/#panel-sources"

    def test_a_never_run_sync_says_so_rather_than_reporting_an_age(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_plan(conn)
        alerts = panels.sidebar(conn, settings, TODAY, NOW).alerts
        assert alerts[0]["text"].startswith("Sync has never run")

    def test_a_fresh_sync_raises_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_run(conn, timedelta(minutes=20))
        a_plan(conn)
        assert panels.sidebar(conn, settings, TODAY, NOW).alerts == []

    def test_a_missing_plan_is_a_gold_alert_linking_the_schedule(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_run(conn, timedelta(minutes=20))
        alerts = panels.sidebar(conn, settings, TODAY, NOW).alerts
        assert [a["level"] for a in alerts] == ["gold"]
        assert alerts[0]["text"].startswith("No plan for today")
        assert alerts[0]["href"] == "/schedule"

    def test_the_alert_reaches_the_rendered_page(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """Derived on page load, never stored — so the only proof it exists is a render.
        Asserted on the whole body: the alerts live in the shell sidebar, which is not
        a panel and therefore not carvable with conftest.panel_slice."""
        conn.commit()
        body = client.get("/").text
        assert "Sync has never run" in body
        assert "k-verm" in body


# ── the brief ─────────────────────────────────────────────────────────────


class TestBriefLines:
    def test_a_stale_ledger_is_stated_above_everything_with_the_failing_source(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.connectors import credentials

        a_run(conn, timedelta(hours=26))
        a_plan(conn)
        credentials.mark_failed(conn, "gmail:personal", "invalid_grant")
        conn.execute(
            "UPDATE credential SET updated_at = ? WHERE source = 'gmail:personal'",
            ((NOW - timedelta(days=2)).isoformat(),),
        )
        section = daily.failure_section(conn, TODAY, settings, NOW)
        line = section.lines[0]
        assert line.text == (
            "Ledger last updated 26h ago — gmail:personal failing since 2d ago. "
            "This brief may be stale."
        )
        assert line.status == "overdue"

    def test_the_stale_line_is_sourced_to_the_run_row(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """B2 has no exception for infrastructure: "your scheduler is dead" is a claim
        about the world, and the reader gets to check it.

        The link used to be `/runs/<id>`, which nothing served — checking the claim
        landed on a 404, which is the same as not being able to check it. It now points
        at the Sources panel, the surface that actually states when each source last ran.
        tests/test_provenance.py holds the general rule; this keeps the run row's own
        reference pinned.
        """
        a_run(conn, timedelta(hours=26))
        a_plan(conn)
        line = daily.failure_section(conn, TODAY, settings, NOW).lines[0]
        assert line.provenance.url("http://x") == "http://x/#panel-sources"

    def test_a_fresh_ledger_says_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_run(conn, timedelta(minutes=20))
        a_plan(conn)
        assert daily.failure_section(conn, TODAY, settings, NOW).lines == []

    def test_a_never_synced_ledger_says_so(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_plan(conn)
        line = daily.failure_section(conn, TODAY, settings, NOW).lines[0]
        assert line.text.startswith("Sync has never run")
        assert line.provenance.url("http://x") == "http://x/#panel-sources"

    def test_a_missing_plan_is_stated_and_sourced_to_the_day_it_was_owed(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_run(conn, timedelta(minutes=20))
        line = daily.failure_section(conn, TODAY, settings, NOW).lines[0]
        assert line.text == "No plan for today — the 05:45 planner did not run."
        assert line.provenance.url("http://x") == "http://x/schedule?date=2026-07-30"

    def test_a_planned_day_says_nothing_about_the_plan(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_run(conn, timedelta(minutes=20))
        a_plan(conn)
        texts = [line.text for line in daily.failure_section(conn, TODAY, settings, NOW).lines]
        assert not any("No plan" in text for text in texts)


class TestOnlySyncRunsCount:
    """The verifier's D3: `run` records every job kind, and counting all of them let one
    roadmap interview reset the staleness clock on all three surfaces at once."""

    def _interview(self, conn: sqlite3.Connection, ago: timedelta) -> None:
        conn.execute(
            "INSERT INTO run (user_id, kind, started_at, finished_at) "
            "VALUES (1, 'interview', ?, ?)",
            ((NOW - ago).isoformat(), (NOW - ago).isoformat()),
        )

    def test_an_interview_run_does_not_hide_a_dead_sync(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_run(conn, timedelta(hours=5))
        self._interview(conn, timedelta(minutes=5))

        beat = heartbeat.read(conn, settings, TODAY, NOW)

        assert beat.stale
        assert beat.age_hours is not None and round(beat.age_hours) == 5

    def test_an_interview_run_alone_still_reads_as_never_synced(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        self._interview(conn, timedelta(minutes=5))
        assert heartbeat.read(conn, settings, TODAY, NOW).never_ran

    def test_a_recent_sync_is_still_fresh_alongside_an_interview(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        # The mirror direction: filtering must not make a healthy sync look dead.
        a_run(conn, timedelta(minutes=10))
        self._interview(conn, timedelta(hours=9))
        beat = heartbeat.read(conn, settings, TODAY, NOW)
        assert not beat.stale
        assert not beat.never_ran

    def test_the_schema_guarantees_a_kind_so_the_filter_cannot_drop_rows(
        self, conn: sqlite3.Connection
    ) -> None:
        # `kind = 'sync'` is only safe as an equality because the column cannot be NULL.
        # If that ever loosens, every pre-existing row silently stops counting and a
        # long-running install reads as never-synced — so pin the constraint here.
        column = next(
            r for r in conn.execute("PRAGMA table_info(run)") if r["name"] == "kind"
        )
        assert column["notnull"] == 1
        assert "sync" in str(column["dflt_value"])
