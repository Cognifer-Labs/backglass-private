"""New pages render on an empty database with declarative empty states. Phase 6."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.web.app import create_app


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn  # migrated db on disk; the app opens its own connections
    return TestClient(create_app(settings))


class TestShell:
    def test_every_page_carries_the_nav_tabs(self, client: TestClient) -> None:
        for path in ("/", "/schedule", "/schedule/week", "/goals"):
            page = client.get(path)
            assert page.status_code == 200, path
            for label in ("Dashboard", "Schedule", "Goals", "People", "Roadmaps"):
                assert label in page.text, (path, label)

    def test_dashboard_content_survived_the_base_refactor(self, client: TestClient) -> None:
        page = client.get("/")
        assert "TODAY" in page.text.upper()
        assert "SOURCES" in page.text.upper()
        assert "review queue" in page.text  # the kbd legend


class TestSidebar:
    """Phase 6 rework: the shell sidebar — alerts derived from state, goal health,
    roadmap progress — present on every page, absent when nothing holds."""

    def test_quiet_state_shows_no_alerts(self, client: TestClient) -> None:
        assert 'id="side-alerts"' not in client.get("/").text

    def test_a_source_failure_alerts_on_every_page(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from backglass.connectors import credentials

        credentials.mark_failed(conn, "gmail:personal", "invalid_grant")
        conn.commit()
        for path in ("/", "/schedule", "/people", "/roadmaps"):
            page = client.get(path).text
            assert "views are incomplete" in page, path
        # An alert derived from state clears with the state — no dismissal machinery.
        conn.execute("UPDATE credential SET status = 'ok'")
        conn.commit()
        assert "views are incomplete" not in client.get("/").text

    def test_review_queue_size_is_an_alert(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, content_hash) VALUES (1, 'gmail:personal', 'x1',"
            " '2026-07-30T08:00:00Z', '2026-07-30T08:00:00Z', 'h1')"
        )
        conn.execute(
            "INSERT INTO commitment (user_id, source_item_id, direction, what,"
            " status, confidence, created_at) VALUES (1, 1, 'i_owe',"
            " 'Send the deck', 'open', 0.4, '2026-07-30T08:05:00Z')"
        )
        conn.commit()
        assert "1 extraction awaiting review" in client.get("/").text

    def test_goal_health_reaches_sidebar_and_panel(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, target_date,"
            " definition_of_done, status, created_at) VALUES (1, 'Ship the fundraise',"
            " 'quarterly', '2026-09-30', 'Round closed', 'active', '2026-07-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at)"
            " VALUES (1, 'cadence', 'Investor conversations', 3, '2026-07-01T00:00:00Z')"
        )
        conn.commit()
        page = client.get("/").text
        # G12: staleness is a chip with a day count (or its no-data words), never a
        # bare colour. G13: risk is a projected date against the target, in words.
        assert "no checkpoints yet" in page
        assert "target is" in page
        assert 'id="side-goals"' in page
    def test_empty_day_names_the_planner_command(self, client: TestClient) -> None:
        page = client.get("/schedule?date=2026-07-30")
        assert page.status_code == 200
        assert "backglass plan" in page.text

    def test_day_shows_blocks_for_the_requested_date(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, generated_at, status)"
            " VALUES ('2026-07-30', 'America/Phoenix', 400, '2026-07-30T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-30T09:00:00-07:00', '2026-07-30T10:30:00-07:00',"
            " 'work', 'Finish deck')"
        )
        conn.commit()
        page = client.get("/schedule?date=2026-07-30")
        assert "Finish deck" in page.text
        assert client.get("/schedule?date=2026-07-31").text.count("Finish deck") == 0

    def test_week_grid_shows_seven_days_and_links_each(self, client: TestClient) -> None:
        page = client.get("/schedule/week?start=2026-07-27")
        assert page.status_code == 200
        for day in range(27, 32):
            assert f"/schedule?date=2026-07-{day}" in page.text
        assert "/schedule?date=2026-08-01" in page.text
        assert "/schedule?date=2026-08-02" in page.text


class TestPeoplePages:
    def test_people_page_renders_empty(self, client: TestClient) -> None:
        page = client.get("/people")
        assert page.status_code == 200
        assert "No profiles match" in page.text

    def test_add_search_and_profile_flow(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        created = client.post(
            "/people",
            data={"name": "Ravi Menon", "role": "Partner", "org": "Stellar Capital",
                  "tags": "investor", "notes": ""},
        )
        assert created.status_code == 200
        assert "Ravi Menon" in created.text

        hit = client.get("/people", params={"q": "stellar"})
        miss = client.get("/people", params={"q": "zzz-nobody"})
        assert "Ravi Menon" in hit.text
        assert "Ravi Menon" not in miss.text

        eid = conn.execute("SELECT id FROM entity").fetchone()["id"]
        profile_page = client.get(f"/people/{eid}")
        assert profile_page.status_code == 200
        assert "Nothing open with Ravi Menon" in profile_page.text
        assert client.get("/people/99999").status_code == 404

    def test_quick_add_appears_on_board(self, client: TestClient) -> None:
        response = client.post(
            "/commitments/quick-add",
            data={"what": "Send Dana the deck", "direction": "i_owe",
                  "counterparty": "Dana", "due_at": "2026-08-05"},
        )
        assert response.status_code == 200
        assert "Send Dana the deck" in response.text


class TestRoadmapPages:
    def test_roadmaps_page_lists_the_four_presets(self, client: TestClient) -> None:
        page = client.get("/roadmaps")
        assert page.status_code == 200
        for path_id in ("founder", "swe", "pm", "medical"):
            assert f"/roadmaps/start/{path_id}" in page.text

    def test_start_step_done_skip_flow(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        started = client.post("/roadmaps/start/medical", follow_redirects=True)
        assert started.status_code == 200
        rid = conn.execute("SELECT id FROM roadmap").fetchone()["id"]
        step = conn.execute(
            "SELECT id FROM roadmap_step WHERE roadmap_id = ? ORDER BY sort_order LIMIT 1",
            (rid,),
        ).fetchone()["id"]

        done = client.post(f"/roadmaps/{rid}/steps/{step}/done")
        assert done.status_code == 200
        assert conn.execute(
            "SELECT status FROM roadmap_step WHERE id = ?", (step,)
        ).fetchone()["status"] == "done"
        assert conn.execute("SELECT COUNT(*) AS n FROM checkpoint").fetchone()["n"] == 1

        second = conn.execute(
            "SELECT id FROM roadmap_step WHERE roadmap_id = ? AND status = 'pending' "
            "ORDER BY sort_order LIMIT 1", (rid,),
        ).fetchone()["id"]
        skipped = client.post(f"/roadmaps/{rid}/steps/{second}/skip")
        assert skipped.status_code == 200
        assert conn.execute(
            "SELECT status FROM roadmap_step WHERE id = ?", (second,)
        ).fetchone()["status"] == "skipped"

    def test_sidebar_tracks_roadmap_progress_on_every_page(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        client.post("/roadmaps/start/medical", follow_redirects=True)
        rid = conn.execute("SELECT id FROM roadmap").fetchone()["id"]
        title = conn.execute("SELECT title FROM roadmap").fetchone()["title"]
        # The sidebar summarizes on pages other than /roadmaps too — that is its point.
        page = client.get("/schedule")
        assert 'id="side-roadmaps"' in page.text
        assert title in page.text
        step = conn.execute(
            "SELECT id FROM roadmap_step WHERE roadmap_id = ? ORDER BY sort_order LIMIT 1",
            (rid,),
        ).fetchone()["id"]
        client.post(f"/roadmaps/{rid}/steps/{step}/done")
        assert "1/" in client.get("/schedule").text

    def test_drop_requires_no_delete(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        client.post("/roadmaps/start/founder", follow_redirects=True)
        rid = conn.execute("SELECT id FROM roadmap").fetchone()["id"]
        dropped = client.post(f"/roadmaps/{rid}/drop", follow_redirects=True)
        assert dropped.status_code == 200
        assert conn.execute(
            "SELECT status FROM roadmap WHERE id = ?", (rid,)
        ).fetchone()["status"] == "dropped"
        # Steps still exist — a drop is a status change, never a delete.
        assert conn.execute("SELECT COUNT(*) AS n FROM roadmap_step").fetchone()["n"] > 0


class TestGoalsPage:
    """Phase 8: goals + checklist get their own page — cards per goal, week tick-grid."""

    def test_renders_empty_states(self, client: TestClient) -> None:
        page = client.get("/goals")
        assert page.status_code == 200
        assert "No checklist items" in page.text
        assert "A goal without a target is inert" in page.text

    def test_goal_card_carries_definition_and_progress(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, target_date, definition_of_done,"
            " status, created_at) VALUES (1, 'Ship the fundraise', 'quarterly',"
            " '2026-09-30', 'Round closed', 'active', '2026-07-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at)"
            " VALUES (1, 'cadence', 'Investor conversations', 3, '2026-07-01T00:00:00Z')"
        )
        conn.commit()
        page = client.get("/goals").text
        assert "Ship the fundraise" in page
        # The definition of done sits on the card — a goal without one is a mood.
        assert "Round closed" in page
        assert "0/3 this week" in page
        assert "quarterly" in page

    def test_checklist_week_grid_ticks_from_the_page(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from backglass.goals import checklist as chk

        item_id = chk.add(conn, "Morning pages")
        conn.commit()
        page = client.get("/goals").text
        assert "Morning pages" in page
        assert f"/goals/checklist/{item_id}/tick" in page

        ticked = client.post(f"/goals/checklist/{item_id}/tick")
        assert ticked.status_code == 200
        assert f"/goals/checklist/{item_id}/untick" in ticked.text
        assert "1/1 today" in ticked.text
        # And back — C2: binary, the row exists or it does not.
        unticked = client.post(f"/goals/checklist/{item_id}/untick")
        assert "0/1 today" in unticked.text
        assert conn.execute("SELECT COUNT(*) AS n FROM checklist_tick").fetchone()["n"] == 0

    def test_streak_is_a_bare_number(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from datetime import date, timedelta

        from backglass.goals import checklist as chk

        item_id = chk.add(conn, "Morning pages")
        today = date.today()
        for back in (1, 2, 3):
            chk.tick(conn, item_id, today - timedelta(days=back))
        conn.commit()
        page = client.get("/goals").text
        assert ">3<" in page  # the streak cell — a number, no copy about keeping it
        assert "streak" in page.lower()

    def test_weekly_buttons_return_the_cards_fragment(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, definition_of_done, status,"
            " created_at) VALUES (1, 'Write daily', 'annual', 'A shipped draft',"
            " 'active', '2026-07-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at)"
            " VALUES (1, 'cadence', 'Deep work sessions', 3, '2026-07-01T00:00:00Z')"
        )
        conn.commit()
        tid = conn.execute("SELECT id FROM target").fetchone()["id"]
        response = client.post(f"/goals/targets/{tid}/weekly/5")
        assert response.status_code == 200
        assert "0/5 this week" in response.text
        assert 'id="goal-cards"' in response.text


class TestScheduleTimeline:
    """Phase 8: the day view is a clock face — positioned entries on an hour ruler."""

    def test_blocks_are_positioned_by_time(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, generated_at, status)"
            " VALUES ('2026-07-28', 'America/Phoenix', 400, '2026-07-28T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T09:00:00-07:00', '2026-07-28T10:30:00-07:00',"
            " 'work', 'Finish deck')"
        )
        conn.commit()
        page = client.get("/schedule?date=2026-07-28").text
        # Ruler starts at 08:00; a 09:00 block sits 60 minutes = 60px down, 90 tall.
        assert "top:60px;height:90px" in page
        assert "09:00–10:30" in page
        assert "08:00" in page  # the hour ruler
        assert 'class="tl"' in page

    def test_capacity_line_reads_as_one_sentence(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, planned_minutes,"
            " overflow_count, generated_at, status) VALUES ('2026-07-28',"
            " 'America/Phoenix', 375, 330, 2, '2026-07-28T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T09:00:00-07:00', '2026-07-28T10:00:00-07:00',"
            " 'work', 'Finish deck')"
        )
        conn.commit()
        page = client.get("/schedule?date=2026-07-28").text
        assert "6h 15m available" in page
        assert "5h 30m planned" in page
        assert "2 did not fit" in page


class TestGoalsKpis:
    """Phase 9: the Excel-dashboard KPI strip — three reels, live through both swaps."""

    def test_strip_counts_match_the_seeded_state(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from datetime import date

        from backglass.goals import checklist as chk

        item_id = chk.add(conn, "Morning pages")
        chk.tick(conn, item_id, date.today())
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, definition_of_done, status,"
            " created_at) VALUES (1, 'Write daily', 'annual', 'A shipped draft',"
            " 'active', '2026-07-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at)"
            " VALUES (1, 'cadence', 'Deep work sessions', 3, '2026-07-01T00:00:00Z')"
        )
        conn.commit()
        page = client.get("/goals").text
        assert 'id="gkpis"' in page
        assert "done today" in page
        assert "best streak" in page
        # One cadence target, zero checkpoints this week: 0 on pace of 1.
        assert "targets on pace" in page
        assert "of 1" in page

    def test_tick_swap_carries_the_strip(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from backglass.goals import checklist as chk

        item_id = chk.add(conn, "Morning pages")
        conn.commit()
        ticked = client.post(f"/goals/checklist/{item_id}/tick").text
        # The strip lives inside #check-week, so the fragment includes it in place.
        assert 'id="gkpis"' in ticked
        assert "hx-swap-oob" not in ticked

    def test_cadence_swap_sends_the_strip_out_of_band(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, definition_of_done, status,"
            " created_at) VALUES (1, 'Write daily', 'annual', 'A shipped draft',"
            " 'active', '2026-07-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at)"
            " VALUES (1, 'cadence', 'Deep work sessions', 3, '2026-07-01T00:00:00Z')"
        )
        conn.commit()
        tid = conn.execute("SELECT id FROM target").fetchone()["id"]
        response = client.post(f"/goals/targets/{tid}/weekly/5").text
        assert 'id="goal-cards"' in response
        assert 'hx-swap-oob="outerHTML"' in response
        assert 'id="gkpis"' in response


class TestConsistencyHeatmap:
    """Phase 9: §5 sequential magnitude — one ink, stepped opacity, numbers on
    every cell. Absent entirely when there is nothing to be consistent about."""

    def test_absent_without_checklist_items(self, client: TestClient) -> None:
        assert "Consistency" not in client.get("/goals").text

    def test_full_day_is_the_darkest_bucket(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from datetime import date, timedelta

        from backglass.goals import checklist as chk

        item_id = chk.add(conn, "Morning pages")
        yesterday = date.today() - timedelta(days=1)
        chk.tick(conn, item_id, yesterday)
        conn.commit()
        page = client.get("/goals").text
        assert "Consistency · last 8 weeks" in page
        assert f'aria-label="1/1 · {yesterday.strftime("%d %b")}"' in page
        assert 'class="hmf b4"' in page

    def test_partial_day_is_a_middle_bucket(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from datetime import date, timedelta

        from backglass.goals import checklist as chk

        first = chk.add(conn, "Morning pages")
        chk.add(conn, "Evening review")
        yesterday = date.today() - timedelta(days=1)
        chk.tick(conn, first, yesterday)
        conn.commit()
        page = client.get("/goals").text
        # 1 of 2 = 50% → bucket 2 of 4.
        assert f'aria-label="1/2 · {yesterday.strftime("%d %b")}"' in page
        assert 'class="hmf b2"' in page

    def test_never_claims_more_than_100_percent(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """Verifier caveat, closed: a tick on an unscheduled day grows the
        denominator; a deactivated item's history leaves the count entirely."""
        from datetime import date, timedelta

        from backglass.goals import checklist as chk

        weekdays_only = chk.add(conn, "Morning pages", weekday_mask=31)
        today = date.today()
        saturday = today - timedelta(days=(today.weekday() - 5) % 7 or 7)
        chk.tick(conn, weekdays_only, saturday)

        retired = chk.add(conn, "Old habit")
        yesterday = today - timedelta(days=1)
        chk.tick(conn, retired, yesterday)
        conn.execute("UPDATE checklist_item SET active = 0 WHERE id = ?", (retired,))
        conn.commit()

        page = client.get("/goals").text
        # Saturday: 0 scheduled, 1 ticked → 1/1, never 1/0.
        assert f'aria-label="1/1 · {saturday.strftime("%d %b")}"' in page
        # Yesterday: the retired item's tick is gone from the numerator.
        assert f'aria-label="0/1 · {yesterday.strftime("%d %b")}"' in page

    def test_future_days_carry_no_claim(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from backglass.goals import checklist as chk

        chk.add(conn, "Morning pages")
        conn.commit()
        page = client.get("/goals").text
        # 8 weeks × 7 days minus scheduled past-and-today cells: the rest are dashes.
        assert 'class="hm"' in page
        assert 'class="hmf b4"' not in page  # nothing ticked yet — no full cell anywhere


class TestWeekAgenda:
    """Phase 9: the week view is seven mini-timelines on one shared ruler."""

    def test_blocks_are_positioned_at_half_scale(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, generated_at, status)"
            " VALUES ('2026-07-28', 'America/Phoenix', 400, '2026-07-28T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T09:00:00-07:00', '2026-07-28T10:30:00-07:00',"
            " 'work', 'Finish deck')"
        )
        conn.commit()
        page = client.get("/schedule/week?start=2026-07-27").text
        # Shared ruler starts at 08:00; 09:00 at 0.5px/min sits 30px down, 45px tall.
        assert "top:30px;height:45px" in page
        assert 'class="wk7"' in page
        assert "Finish deck" in page

    def test_capacity_line_per_day(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, planned_minutes,"
            " overflow_count, generated_at, status) VALUES ('2026-07-28',"
            " 'America/Phoenix', 375, 330, 2, '2026-07-28T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T09:00:00-07:00', '2026-07-28T10:00:00-07:00',"
            " 'work', 'Finish deck')"
        )
        conn.commit()
        page = client.get("/schedule/week?start=2026-07-27").text
        assert "5h30 planned" in page
        assert "0h45 free" in page
        assert "2 over" in page
        # Unplanned days state their emptiness, not fake zeros.
        assert "—" in page


class TestDueLabel:
    """Due dates render in words sized to their distance, not raw ISO."""

    def test_within_a_week_is_a_weekday(self) -> None:
        from datetime import date

        from backglass.web.panels import due_label

        today = date(2026, 7, 30)  # a Thursday
        assert due_label("2026-07-31", today) == "due Fri"
        assert due_label("2026-08-05", today) == "due Wed"

    def test_beyond_a_week_is_day_month_with_year_only_when_it_differs(self) -> None:
        from datetime import date

        from backglass.web.panels import due_label

        today = date(2026, 7, 30)
        assert due_label("2026-09-14", today) == "due 14 Sep"
        assert due_label("2027-01-05", today) == "due 05 Jan 2027"
        # A past date never reaches this label in the UI (state chips own overdue),
        # but the function still degrades to the dated form rather than a weekday.
        assert due_label("2026-07-20", today) == "due 20 Jul"
        assert due_label(None, today) == "no date"
