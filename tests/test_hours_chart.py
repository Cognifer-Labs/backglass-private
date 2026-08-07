"""Logged by month — the log zone's time dimension.

The bars say how far each accumulator is; this says whether logging is still
happening. The tests that matter are the bucketing rule (a stamp is filed under the
month it names in its own offset, never converted) and the fragment case (the chart
is built in the one context builder every `#roadmap-totals` swap goes through, so a
logged hour re-renders it instead of swapping it away).
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.goals import hours
from backglass.web.app import create_app

TODAY = date(2026, 8, 6)


def _total(done: int = 0, goal: int = 0, *stamps: tuple[str, int]) -> dict[str, object]:
    return {
        "done": done,
        "total_count": goal,
        "entries": [{"occurred_at": s, "delta": d} for s, d in stamps],
    }


class TestBucketing:
    def test_a_stamp_is_filed_under_the_month_its_own_offset_names(self) -> None:
        """The owner moves between UTC-7 and UTC+5:30, so the two instants either
        side of a month boundary are the case. 20:00 on 31 July in Phoenix is 03:00
        on 1 August in UTC, and 01:00 on 1 August in Kolkata is 19:30 on 31 July —
        converting either one files an hour under a month nobody worked it in."""
        got = hours.monthly(
            [_total(0, 0, ("2026-07-31T20:00:00-07:00", 4), ("2026-08-01T01:00:00+05:30", 3))],
            today=TODAY,
        )
        by_month = {(m.year, m.month): m.hours for m in got.months}
        assert by_month[(2026, 7)] == 4
        assert by_month[(2026, 8)] == 3

    def test_entries_older_than_the_window_are_left_out(self) -> None:
        got = hours.monthly(
            [_total(0, 0, ("2025-08-06T09:00:00-07:00", 9), ("2025-09-06T09:00:00-07:00", 2))],
            today=TODAY,
            months=12,
        )
        # The window is the twelve months ending with today's: Sep 2025 → Aug 2026.
        assert [(m.year, m.month) for m in got.months][0] == (2025, 9)
        assert got.logged == 2

    def test_every_total_feeds_one_strip(self) -> None:
        got = hours.monthly(
            [
                _total(0, 0, ("2026-08-01T09:00:00-07:00", 4)),
                _total(0, 0, ("2026-08-02T09:00:00-07:00", 6)),
            ],
            today=TODAY,
        )
        assert got.logged == 10
        assert got.peak == 10

    def test_an_unparseable_stamp_is_skipped_not_raised(self) -> None:
        """Rule 5's unit here is the row: one malformed stamp in a four-year ledger
        must not take the chart down, and its hours are still in the bar above."""
        got = hours.monthly(
            [_total(0, 0, ("not a timestamp", 5), ("2026-08-02T09:00:00-07:00", 6))],
            today=TODAY,
        )
        assert got.logged == 6

    def test_nothing_logged_reports_no_data(self) -> None:
        assert hours.monthly([_total(0, 60)], today=TODAY).has_data is False


class TestPace:
    def test_pace_is_the_remainder_over_the_months_left(self) -> None:
        got = hours.monthly(
            [_total(20, 60), _total(0, 40)], today=TODAY, target_date="2026-12-01"
        )
        # 40 + 40 remaining over four whole months (Aug → Dec), rounded up.
        assert got.remaining == 80
        assert got.pace == 20

    def test_the_axis_lifts_so_the_pace_line_stays_inside_the_plot(self) -> None:
        """A pace far above every bar is exactly the case worth seeing, and a
        reference line drawn above the plot cannot be seen at all."""
        got = hours.monthly(
            [_total(0, 200, ("2026-08-02T09:00:00-07:00", 3))],
            today=TODAY,
            target_date="2026-10-01",
        )
        assert got.peak == 3
        assert got.pace == 100
        assert got.axis_max == 100
        assert got.pace_pct == pytest.approx(100.0)

    @pytest.mark.parametrize("target", [None, "", "2026-01-01", "not-a-date"])
    def test_no_pace_without_a_future_target(self, target: str | None) -> None:
        got = hours.monthly([_total(0, 60)], today=TODAY, target_date=target)
        assert got.pace is None
        assert got.pace_pct == 0.0

    def test_no_pace_once_nothing_is_remaining(self) -> None:
        got = hours.monthly([_total(60, 60)], today=TODAY, target_date="2027-06-01")
        assert got.remaining == 0
        assert got.pace is None

    def test_a_target_inside_this_month_asks_for_the_whole_remainder(self) -> None:
        got = hours.monthly([_total(0, 15)], today=TODAY, target_date="2026-08-20")
        assert got.pace == 15


class TestColumnHeights:
    def test_a_month_with_something_in_it_never_draws_as_a_month_with_nothing(self) -> None:
        got = hours.monthly(
            [_total(0, 0, ("2026-08-02T09:00:00-07:00", 90), ("2026-07-02T09:00:00-07:00", 1))],
            today=TODAY,
        )
        by_month = {(m.year, m.month): m for m in got.months}
        assert by_month[(2026, 8)].pct == pytest.approx(100.0)
        assert by_month[(2026, 7)].pct == hours.MIN_BAR_PCT
        assert by_month[(2026, 6)].pct == 0.0

    def test_the_current_month_is_the_one_marked(self) -> None:
        got = hours.monthly([_total(0, 0, ("2026-08-02T09:00:00-07:00", 2))], today=TODAY)
        assert [m for m in got.months if m.is_current] == [got.months[-1]]
        assert got.months[-1].full == "Aug 2026"


class TestOnThePage:
    @pytest.fixture
    def client(self, conn: sqlite3.Connection, settings: Settings) -> TestClient:
        del conn
        return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    def _start_medical(self, client: TestClient, conn: sqlite3.Connection) -> tuple[int, int]:
        client.post("/roadmaps/start/medical", follow_redirects=True)
        rid = conn.execute("SELECT id FROM roadmap").fetchone()["id"]
        tid = conn.execute(
            "SELECT t.id FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
            "WHERE r.id = ? AND t.kind = 'total' ORDER BY t.id LIMIT 1",
            (rid,),
        ).fetchone()["id"]
        return rid, tid

    def test_an_unlogged_roadmap_draws_no_empty_axis(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, _ = self._start_medical(client, conn)
        assert 'class="mplot"' not in client.get(f"/roadmaps/{rid}").text

    def test_a_logged_hour_puts_the_month_on_the_page(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        client.post(f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "4", "note": ""})
        page = client.get(f"/roadmaps/{rid}").text
        assert 'class="mplot"' in page
        assert "Logged by month" in page
        assert "4 in 12 months" in page

    def test_the_chart_survives_the_swap_that_produced_it(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """`#roadmap-totals` is replaced wholesale by every write here. A chart
        built anywhere but the shared context builder renders on GET and vanishes
        on the first logged hour — so the assertion is on the POST's own body."""
        rid, tid = self._start_medical(client, conn)
        body = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "4", "note": ""}
        ).text
        assert 'class="mplot"' in body
        # And it moves: a second hour in the same month re-renders a bigger figure.
        again = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "6", "note": ""}
        ).text
        assert "10 in 12 months" in again

    def test_the_pace_line_is_drawn_against_a_dated_goal(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        page = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "1", "note": ""}
        ).text
        assert 'class="mpace"' in page
        assert "/mo to target" in page

    def test_the_activity_registry_is_a_table_with_a_rollup(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        client.post(
            f"/roadmaps/{rid}/activities",
            data={"title": "ED scribe", "org": "Banner", "role": "Scribe",
                  "category": "clinical"},
        )
        aid = conn.execute("SELECT id FROM activity").fetchone()["id"]
        page = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log",
            data={"amount": "7", "note": "", "activity_id": str(aid)},
        ).text
        assert 'class="wkg atbl"' in page
        for header in ("Activity", "Category", "Where", "Hours", "Entries", "Span"):
            assert f">{header}<" in page
        assert "<tfoot>" in page
        assert "1 activity" in page
        assert "1 of 15 slots" in page
        assert "7 h" in page


class TestUnfiledHours:
    """Hours the bars count and the registry cannot.

    The table made the gap visible by contradiction — "0 of 15 slots · 0 h" above a
    54-hour bar — so the page now states it, and the log form stops producing more of
    it where the answer is not ambiguous.
    """

    @pytest.fixture
    def client(self, conn: sqlite3.Connection, settings: Settings) -> TestClient:
        del conn
        return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    def _start_medical(self, client: TestClient, conn: sqlite3.Connection) -> tuple[int, int]:
        client.post("/roadmaps/start/medical", follow_redirects=True)
        rid = conn.execute("SELECT id FROM roadmap").fetchone()["id"]
        tid = conn.execute(
            "SELECT t.id FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
            "WHERE r.id = ? AND t.kind = 'total' AND t.title LIKE 'Clinical%'",
            (rid,),
        ).fetchone()["id"]
        return rid, tid

    def _add(self, client: TestClient, rid: int, title: str, category: str) -> None:
        client.post(f"/roadmaps/{rid}/activities", data={"title": title, "category": category})

    def test_an_hour_naming_no_activity_is_counted_and_said(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        page = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "6", "note": ""}
        ).text
        assert "6 unfiled" in page
        assert "cannot appear in Work &amp; Activities" in page
        # The repair named is the one that keeps the dates — the log form has no date
        # field, so an unlog-and-relog here would move the entry to today.
        assert "backglass log --on" in page

    def test_nothing_is_said_when_every_hour_is_filed(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        self._add(client, rid, "ED scribe", "clinical")
        aid = conn.execute("SELECT id FROM activity").fetchone()["id"]
        page = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log",
            data={"amount": "6", "note": "", "activity_id": str(aid)},
        ).text
        assert "unfiled" not in page
        assert "cannot appear in Work" not in page

    def test_the_count_is_this_goal_s_hours_not_every_goal_s(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """A second active goal's total counts calls, not hours. A global
        `activity_id IS NULL` sum would fold them into a figure this page calls
        hours; summing the goal's own entry rows cannot."""
        rid, tid = self._start_medical(client, conn)
        # A second live goal with its own accumulator, counting calls rather than
        # hours, and an entry on it that names no activity either.
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, target_date, definition_of_done,"
            " status, created_at) VALUES (1, 'Raise a seed round', 'annual',"
            " '2027-06-01', 'Termsheet signed', 'active', '2026-07-01T00:00:00Z')"
        )
        other_goal = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO target (user_id, goal_id, title, kind, total_count, active,"
            " created_at) VALUES (1, ?, 'Investor conversations', 'total', 40, 1,"
            " '2026-07-01T00:00:00Z')",
            (other_goal,),
        )
        other_tid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO checkpoint (user_id, target_id, occurred_at, source, delta)"
            " VALUES (1, ?, '2026-08-02T09:00:00-07:00', 'manual', 9)",
            (other_tid,),
        )
        conn.commit()
        page = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "6", "note": ""}
        ).text
        assert "6 unfiled" in page
        assert "15 unfiled" not in page

    def test_the_only_activity_a_total_can_mean_is_preselected(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        self._add(client, rid, "ED scribe", "clinical")
        aid = conn.execute("SELECT id FROM activity").fetchone()["id"]
        page = client.get(f"/roadmaps/{rid}").text
        assert f'<option value="{aid}" selected>' in page
        # ...and "no activity" gives up its selected state to it, or the browser
        # keeps showing the first option while the form posts the second.
        assert page.count('<option value="0" selected>no activity</option>') < 5

    def test_two_claimants_on_one_total_preselect_neither(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """A wrong attribution is visible and removable; an ambiguous one guessed for
        the owner is neither. Two clinical activities mean no default."""
        rid, _ = self._start_medical(client, conn)
        self._add(client, rid, "ED scribe", "clinical")
        self._add(client, rid, "Free clinic", "clinical")
        page = client.get(f"/roadmaps/{rid}").text
        ids = [r["id"] for r in conn.execute("SELECT id FROM activity ORDER BY id")]
        for aid in ids:
            assert f'<option value="{aid}" selected>' not in page

    def test_a_third_activity_does_not_resurrect_the_first_one_s_claim(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """The ambiguity tombstone has to survive later rows: with three clinical
        activities the map must still hold no default for that total."""
        rid, _ = self._start_medical(client, conn)
        for title in ("ED scribe", "Free clinic", "Hospice volunteer"):
            self._add(client, rid, title, "clinical")
        page = client.get(f"/roadmaps/{rid}").text
        ids = [r["id"] for r in conn.execute("SELECT id FROM activity ORDER BY id")]
        for aid in ids:
            assert f'<option value="{aid}" selected>' not in page
