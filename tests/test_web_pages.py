"""New pages render on an empty database with declarative empty states. Phase 6."""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.web.app import create_app
from tests.conftest import panel_slice


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn  # migrated db on disk; the app opens its own connections
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


@pytest.fixture(autouse=True)
def sync_is_alive(conn: sqlite3.Connection) -> None:
    """A migrated-but-never-synced ledger raises its own vermilion alert (heartbeat.py).
    Correct, and not what these tests are about — they assume the scheduler is alive.
    The alert itself is covered in tests/test_heartbeat.py."""
    from tests.conftest import healthy_run

    healthy_run(conn)
    conn.commit()


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

    def test_quiet_state_shows_no_alerts(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """"Quiet" means the whole scheduler ran, and the planner is part of it.

        `heartbeat._plan_due` turns true once the local clock passes 05:45 plus grace on a
        working day, so without today's plan this assertion passes all weekend and fails
        on Monday morning. It was green for an entire session and went red at 07:41 on a
        Monday, which is the tell for a test that depends on the wall clock rather than on
        what it is asserting. Established here rather than in the module fixture, because
        an extra day_plan row shifts every rowid the other tests in this file assume.
        """
        from tests.conftest import todays_plan

        todays_plan(conn, settings)
        conn.commit()
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
        # Format audit: the dashboard panel is a per-goal summary — G12 chip and
        # week aggregate, no G13 prose. The sentence lives on /goals, and the
        # sidebar GOALS block appears on pages that don't already show the state.
        page = client.get("/").text
        assert "no checkpoints yet" in page
        assert "0/3 this week" in page
        assert "target is" not in page
        goals_page = client.get("/goals").text
        assert "target is" in goals_page
        elsewhere = client.get("/schedule").text
        assert 'id="side-goals"' in elsewhere
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
        # Derived, not hardcoded to 1. The autouse fixture writes today's plan first, so
        # a literal id silently attached this block to *that* plan and the assertion
        # failed a long way from the cause.
        plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (?, '2026-07-30T09:00:00-07:00', '2026-07-30T10:30:00-07:00',"
            " 'work', 'Finish deck')",
            (plan_id,),
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

    def test_a_persons_plans_render_on_their_page(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """Driven through the real route, not the query.

        tasks/lessons.md keeps recording the same shape: a check written against the
        function passes while the door a person actually uses walks past it. The page is
        the door here, and it is the one that has to show the plan.
        """
        conn.execute(
            "INSERT INTO entity (kind, canonical_name, aliases_json, tags_json)"
            " VALUES ('person', 'Priya Raman', '[]', '[]')"
        )
        entity_id = int(conn.execute("SELECT id FROM entity").fetchone()["id"])
        conn.execute(
            "INSERT INTO source_item (source, external_id, fetched_at, occurred_at,"
            " author, title, body_text, content_hash, triage_verdict)"
            " VALUES ('imessage', 'p1', '2026-07-20T09:00:00-07:00',"
            " '2026-07-20T09:00:00-07:00', 'Priya', 'dinner', 'b', 'ph1', 'keep')"
        )
        source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
            " when_is_explicit, location, status, confidence, source_item_id, created_at)"
            " VALUES (1, 'social', 'dinner downtown', '2099-08-05', NULL, 1,"
            " 'Fifth Street', 'proposed', 0.9, ?, '2026-07-20T09:00:00-07:00')",
            (source_id,),
        )
        engagement_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO engagement_person (user_id, engagement_id, entity_id)"
            " VALUES (1, ?, ?)",
            (engagement_id, entity_id),
        )

        page = client.get(f"/people/{entity_id}")

        assert page.status_code == 200
        plans = panel_slice(page.text, "panel-plans")
        assert "dinner downtown" in plans
        assert "Fifth Street" in plans
        # Known socially, and through iMessage — derived from the evidence, not typed.
        derived = panel_slice(page.text, "panel-derived")
        assert "Seen socially" in derived
        assert "imessage" in derived

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


class TestRoadmapReplan:
    """The judged replan of the two roadmap surfaces: a masthead whose reels stay
    inside §7's budget, a log zone first, a timetable that reads as a timetable
    rather than a control grid, and a list that answers "where does this stand"."""

    def _start(
        self, client: TestClient, conn: sqlite3.Connection, path: str = "medical"
    ) -> int:
        client.post(f"/roadmaps/start/{path}", follow_redirects=True)
        return int(conn.execute("SELECT id FROM roadmap").fetchone()["id"])

    def _steps(self, conn: sqlite3.Connection, rid: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT id, title FROM roadmap_step WHERE roadmap_id = ? ORDER BY sort_order",
            (rid,),
        ).fetchall()

    def _redate(self, conn: sqlite3.Connection, step_id: int, iso: str) -> None:
        conn.execute("UPDATE roadmap_step SET planned_date = ? WHERE id = ?", (iso, step_id))
        conn.commit()

    def test_year_headings_appear_only_when_the_timetable_spans_years(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid = self._start(client, conn, "swe")
        steps = self._steps(conn, rid)
        for i, s in enumerate(steps):
            self._redate(conn, s["id"], f"2027-0{1 + i % 9}-05")
        page = client.get(f"/roadmaps/{rid}").text
        assert "yrhd" not in page  # one year: a heading over every row is wallpaper

        self._redate(conn, steps[-1]["id"], "2028-02-05")
        page = client.get(f"/roadmaps/{rid}").text
        assert '<p class="yrhd">2027</p>' in page
        assert '<p class="yrhd">2028</p>' in page

    def test_next_step_carries_the_rule_and_black_is_spent_only_on_today(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.brief.daily import today_in

        rid = self._start(client, conn)
        steps = self._steps(conn, rid)
        today = today_in(settings.default_tz)
        # The next step is a year out, one step falls literally today, one is in
        # the past, the rest are further out. Only the literal today earns the
        # black chip — a date is not a state, and "next" is not "due".
        for s in steps:
            self._redate(conn, s["id"], today.replace(year=today.year + 2).isoformat())
        self._redate(conn, steps[0]["id"], today.replace(year=today.year + 1).isoformat())
        self._redate(conn, steps[1]["id"], today.isoformat())
        self._redate(conn, steps[2]["id"], today.replace(year=today.year - 1).isoformat())
        page = client.get(f"/roadmaps/{rid}").text

        # The NEXT marker is a rule plus a label on the first pending step, never
        # an inverted row and never the black chip.
        assert page.count('<span class="nextlbl">Next</span>') == 1
        assert page.count("nextstep") == 1
        assert steps[0]["title"] in page
        assert page.count("Due today") == 1
        assert page.count("Overdue") == 1

    def test_open_steps_collapse_to_two_boxes_plus_quiet_text(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid = self._start(client, conn)
        page = client.get(f"/roadmaps/{rid}").text
        steps = self._steps(conn, rid)
        assert len(steps) == 8

        # Exactly two boxed buttons on the whole timetable — Done and Skip on the
        # next step. Every other open step keeps both actions as text links.
        assert page.count('class="btn ok" hx-post="/roadmaps/') == 1
        assert page.count('class="btn defer"') == 1
        for step in steps[1:]:
            for action in ("done", "skip"):
                assert (
                    f'class="lnk" hx-post="/roadmaps/{rid}/steps/{step["id"]}/{action}"'
                    in page
                )
        # Each step is one expandable timeline item whose panel holds re-date,
        # reorder and the edit form; nothing was removed, so a batch replan is
        # still one tap per action.
        assert page.count('<details class="stepx') == len(steps)
        for step in steps:
            assert f"/roadmaps/{rid}/steps/{step['id']}/date/" in page
            assert f"/roadmaps/{rid}/steps/{step['id']}/move/up" in page

    def test_masthead_spends_at_most_two_reels(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid = self._start(client, conn)  # medical carries five lifetime totals
        page = client.get(f"/roadmaps/{rid}").text
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
            "WHERE r.id = ? AND t.kind = 'total'", (rid,)
        ).fetchone()["n"] == 5
        assert page.count('<span class="reel">') == 2
        # G11 stays intact: the two health signals never merge into one score.
        assert 'class="rstale"' in page or "no checkpoints yet" not in page

    def test_step_writes_refresh_the_masthead_out_of_band(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid = self._start(client, conn)
        step = self._steps(conn, rid)[0]["id"]
        fragment = client.post(f"/roadmaps/{rid}/steps/{step}/done").text
        # The masthead moved above both swap targets, so the fragment carries it.
        assert 'id="roadmap-masthead"' in fragment
        assert 'hx-swap-oob="outerHTML"' in fragment
        assert "1/8 steps" in fragment

    def test_list_rows_carry_the_next_step_and_the_headline_total(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid = self._start(client, conn)
        tid = conn.execute(
            "SELECT t.id FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
            "WHERE r.id = ? AND t.kind = 'total' ORDER BY t.id LIMIT 1", (rid,)
        ).fetchone()["id"]
        client.post(f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "4", "note": ""})

        page = client.get("/roadmaps").text
        assert "1 active" in page
        assert "Next: " + self._steps(conn, rid)[0]["title"] in page
        assert "0/8 steps" in page
        assert "Shadowing hours" in page
        assert "4/75" in page

    def test_closed_roadmaps_sink_to_their_own_group(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        page = client.get("/roadmaps").text
        assert "Closed" not in page  # nothing started: no empty group either

        rid = self._start(client, conn)
        page = client.get("/roadmaps").text
        assert "rmclosed\">—" in page  # absence is an em-dash, never a sentence

        client.post(f"/roadmaps/{rid}/drop", follow_redirects=True)
        page = client.get("/roadmaps").text
        assert "0 active" in page
        assert "rmclosed" in page and "dropped" in page

    def test_a_live_path_cannot_be_started_twice(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid = self._start(client, conn)
        page = client.get("/roadmaps").text
        # The button is replaced by a link to what already exists.
        assert "/roadmaps/start/medical" not in page
        assert f'href="/roadmaps/{rid}">started →' in page
        assert "/roadmaps/start/swe" in page  # untouched paths still start

        # Structural, not cosmetic: posting the start anyway redirects instead of
        # instantiating a second goal and its cadences.
        again = client.post("/roadmaps/start/medical", follow_redirects=False)
        assert again.status_code == 303
        assert again.headers["location"] == f"/roadmaps/{rid}"
        assert conn.execute("SELECT COUNT(*) AS n FROM roadmap").fetchone()["n"] == 1

        # Dropping releases the path — restart is possible, but only after that.
        client.post(f"/roadmaps/{rid}/drop", follow_redirects=True)
        assert "/roadmaps/start/medical" in client.get("/roadmaps").text

    def test_cadences_are_read_only_with_a_live_weekly_count(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid = self._start(client, conn)
        page = client.get(f"/roadmaps/{rid}").text
        assert "0/5</span> this week" in page  # MCAT practice sections, 5/wk
        assert "tick on Goals →" in page


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
        # The replan bans verdict chips: capacity is a sentence and the only chip on
        # the page is the gold overflow warning.
        assert "FITS" not in page
        assert "FULLY BOOKED" not in page

    def test_overflow_chip_stays_count_only(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """Staged: the DID NOT FIT table waits on the planner persisting bumped
        items. Until then the count is the whole honest claim — no faked list."""
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
        assert "2 did not fit" in page
        assert "EST" not in page  # no staged table columns
        assert "did not fit</summary>" not in page  # not a disclosure yet

    def test_tiny_entries_drop_their_title_to_the_title_attribute(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """Under 32px there is no room for a readable line, so the words move to the
        title attribute rather than under the 11px type floor."""
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, generated_at, status)"
            " VALUES ('2026-07-28', 'America/Phoenix', 400, '2026-07-28T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T09:00:00-07:00', '2026-07-28T09:20:00-07:00',"
            " 'small', 'Inbox sweep')"
        )
        conn.commit()
        page = client.get("/schedule?date=2026-07-28").text
        assert "height:20px" in page
        assert "tiny" in page
        assert 'title="09:00–09:20 Inbox sweep"' in page
        # The title is not drawn on the canvas: only the attributes carry it.
        assert ">Inbox sweep<" not in page

    def test_now_next_strip_renders_only_for_today(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.brief.daily import today_in

        day = today_in(settings.default_tz)
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, generated_at,"
            " status) VALUES (?, 'America/Phoenix', 400, '2026-07-28T05:50:00',"
            " 'accepted')",
            (day.isoformat(),),
        )
        # A block spanning the whole day so "now" lands inside it whatever the clock.
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, ?, ?, 'work', 'All day long')",
            (f"{day.isoformat()}T00:00:00-07:00", f"{day.isoformat()}T23:59:00-07:00"),
        )
        conn.commit()
        assert "NOW</span>" in client.get(f"/schedule?date={day.isoformat()}").text
        other = client.get("/schedule?date=2026-07-28").text
        assert "NOW</span>" not in other
        assert "NEXT</span>" not in other

    def test_gaps_disclosure_lists_the_free_intervals(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, generated_at, status)"
            " VALUES ('2026-07-28', 'America/Phoenix', 400, '2026-07-28T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T09:00:00-07:00', '2026-07-28T10:15:00-07:00',"
            " 'work', 'Finish deck')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T11:30:00-07:00', '2026-07-28T12:00:00-07:00',"
            " 'small', 'Inbox sweep')"
        )
        conn.commit()
        page = client.get("/schedule?date=2026-07-28").text
        # 08:00–09:00, 10:15–11:30 and 12:00–19:00 are free; the ruler bounds the ends.
        assert "Gaps · 3 free intervals" in page
        assert "08:00–09:00 · 1h 00m free" in page
        assert "10:15–11:30 · 1h 15m free" in page
        assert "12:00–19:00 · 7h 00m free" in page

    def test_fragmented_day_says_so_in_words(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """P9's sentence is the product, so it renders as body text, never a chip
        and never folded. It is re-derived from the absence of a protected block."""
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, generated_at, status)"
            " VALUES ('2026-07-28', 'America/Phoenix', 400, '2026-07-28T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T09:00:00-07:00', '2026-07-28T10:00:00-07:00',"
            " 'work', 'Finish deck')"
        )
        conn.commit()
        assert (
            "No deep work block available today, calendar is fragmented"
            in client.get("/schedule?date=2026-07-28").text
        )
        conn.execute(
            "UPDATE plan_block SET kind = 'protected' WHERE title = 'Finish deck'"
        )
        conn.commit()
        assert (
            "No deep work block available today, calendar is fragmented"
            not in client.get("/schedule?date=2026-07-28").text
        )

    def test_a_day_under_the_capacity_floor_says_fully_booked(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, generated_at, status)"
            " VALUES ('2026-07-28', 'America/Phoenix', 30, '2026-07-28T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T09:00:00-07:00', '2026-07-28T17:00:00-07:00',"
            " 'fixed', 'Conference')"
        )
        conn.commit()
        page = client.get("/schedule?date=2026-07-28").text
        assert "Fully booked — only due items listed" in page
        # A sentence, not a chip: no new ink is spent on a verdict.
        assert "chip k-black" not in page


def _cadence_goal(
    conn: sqlite3.Connection, title: str = "Write daily", weekly: int = 3
) -> int:
    """One active goal with one cadence target. Returns the target id."""
    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, definition_of_done, status,"
        " created_at) VALUES (1, ?, 'annual', 'A shipped draft',"
        " 'active', '2026-07-01T00:00:00Z')",
        (title,),
    )
    gid = conn.execute("SELECT id FROM goal ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO target (goal_id, kind, title, weekly_count, created_at)"
        " VALUES (?, 'cadence', 'Deep work sessions', ?, '2026-07-01T00:00:00Z')",
        (gid, weekly),
    )
    conn.commit()
    return int(
        conn.execute("SELECT id FROM target ORDER BY id DESC LIMIT 1").fetchone()["id"]
    )


class TestGoalsTodayTicks:
    """Replan §2: today's column is the first thing on the page and its own swap
    target, banner count included."""

    def test_today_rows_carry_a_box_and_a_bare_streak(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from datetime import date, timedelta

        from backglass.goals import checklist as chk

        item_id = chk.add(conn, "Morning pages")
        today = date.today()
        for back in (1, 2, 3):
            chk.tick(conn, item_id, today - timedelta(days=back))
        conn.commit()
        page = client.get("/goals").text
        assert 'id="today-ticks"' in page
        # The tappable box posts the existing endpoint and swaps the ticks section.
        assert f'hx-post="/goals/checklist/{item_id}/tick"' in page
        assert 'hx-target="#today-ticks"' in page
        # C4: the streak is a number in its own cell, no copy around it.
        assert '<span class="tts" title="Streak, days">3</span>' in page

    def test_unscheduled_today_says_so_without_an_empty_grid(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from datetime import date

        from backglass.goals import checklist as chk

        # Scheduled on every weekday except today.
        mask = 127 - (1 << date.today().weekday())
        chk.add(conn, "Gym", weekday_mask=mask)
        conn.commit()
        page = client.get("/goals").text
        assert "Nothing on the checklist today" in page
        assert "0/0 today" in page

    def test_tick_response_refreshes_the_week_grid_out_of_band(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from backglass.goals import checklist as chk

        item_id = chk.add(conn, "Morning pages")
        conn.commit()
        ticked = client.post(f"/goals/checklist/{item_id}/tick").text
        assert 'id="today-ticks"' in ticked
        assert 'id="week-grid" hx-swap-oob="outerHTML"' in ticked
        assert "1/1 today" in ticked


class TestGoalsThisWeekFold:
    """Replan §3: THIS WEEK folds, and the fold survives a tick structurally —
    the <details> is outside every swap target, so no swap can reset it."""

    def test_details_wrapper_is_outside_the_swapped_ids(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from backglass.goals import checklist as chk

        chk.add(conn, "Morning pages")
        _cadence_goal(conn)
        page = client.get("/goals").text

        # The details element opens before #week-grid and closes after it: the swap
        # target is INSIDE the fold, never the other way round.
        fold = page.index('<details class="wkfold">')
        grid = page.index('id="week-grid"')
        assert fold < grid < page.index("</details>", fold)
        # And it is in neither of the page's other two swap targets.
        assert fold < page.index('id="goal-cards"')
        ticks_open = page.index('id="today-ticks"')
        assert not ticks_open < fold < page.index("</section>", ticks_open)

    def test_summary_carries_the_on_pace_count(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _cadence_goal(conn)
        page = client.get("/goals").text
        assert "This week ·" in page
        # One cadence target, no checkpoints this week: 0 of 1 on pace.
        assert '<span class="pace" id="wk-pace">0/1 targets on pace</span>' in page
        # The three-reel strip is retired; its other two numbers were duplicates.
        assert 'id="gkpis"' not in page
        assert "best streak" not in page

    def test_cadence_write_sends_only_the_pace_span_out_of_band(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        tid = _cadence_goal(conn)
        response = client.post(f"/goals/targets/{tid}/weekly/5").text
        assert 'id="goal-cards"' in response
        assert 'id="wk-pace" hx-swap-oob="outerHTML"' in response
        # No <details> rides along: nothing in this response can move the fold.
        assert "<details" not in response.split('id="goal-cards"')[0]


class TestGoalsAttentionSplit:
    """Replan §4/§5: flagged goals render the full card, healthy ones collapse to a
    row that discloses the same card."""

    def _behind(self, conn: sqlite3.Connection) -> int:
        return _cadence_goal(conn, "Write daily", weekly=3)

    def test_behind_goal_lands_in_needs_attention_with_the_full_card(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        self._behind(conn)
        page = client.get("/goals").text
        assert "Needs attention" in page
        # Full card markup: definition of done, the track bar, the /wk buttons.
        assert "A shipped draft" in page
        assert "0/3 this week" in page
        assert "3/wk" in page
        assert "All goals on track" not in page

    def test_on_pace_goal_collapses_to_one_row_that_still_holds_its_card(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from datetime import date

        from backglass.goals import checkpoints

        tid = _cadence_goal(conn, "Write daily", weekly=1)
        checkpoints.record(conn, tid, source="manual", occurred_at=date.today().isoformat())
        conn.commit()
        page = client.get("/goals").text
        assert "Needs attention" not in page
        assert "All goals on track" in page
        assert '<details class="goalrow">' in page
        # The row states the next tick target and the last checkpoint date.
        assert "Deep work sessions 1/1 this week" in page
        assert f"last {date.today().isoformat()}" in page
        # And the same full card is disclosed inside it — same markup, both lists.
        assert "A shipped draft" in page
        assert "3/wk" in page

    def test_absence_is_an_em_dash_not_a_sentence(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _cadence_goal(conn)
        page = client.get("/goals").text
        # The target line: "0/3 this week · last —", never a wallpaper sentence.
        assert "0/3 this week · last —" in " ".join(page.split())
        # G12's goal-level chip keeps its own wording — it is a day count, not a
        # repeated row, and "no checkpoints yet" is the honest reading of never.
        assert "no checkpoints yet" in page

    def test_inert_goals_collapse_to_one_counted_line(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        for title in ("Learn guitar", "Read more"):
            conn.execute(
                "INSERT INTO goal (user_id, title, horizon, definition_of_done, status,"
                " created_at) VALUES (1, ?, 'annual', 'Someday', 'active',"
                " '2026-07-01T00:00:00Z')",
                (title,),
            )
        _cadence_goal(conn)
        page = client.get("/goals").text
        assert "2 goals without targets — a goal without a target is inert" in page
        assert "Learn guitar" in page  # named inside the disclosure, not lost

    def test_roadmap_backed_goal_links_across_instead_of_merging(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _cadence_goal(conn)
        gid = conn.execute("SELECT id FROM goal").fetchone()["id"]
        conn.execute(
            "INSERT INTO roadmap (user_id, path_id, path_version, title, goal_id,"
            " status, created_at) VALUES (1, 'premed', '1', 'Pre-med', ?, 'active',"
            " '2026-07-01T00:00:00Z')",
            (gid,),
        )
        conn.commit()
        rid = conn.execute("SELECT id FROM roadmap").fetchone()["id"]
        page = client.get("/goals").text
        assert f'href="/roadmaps/{rid}"' in page
        # Fixed label: the roadmap shares the goal's name, so echoing the title
        # under the title read as a stutter.
        assert "roadmap &amp; step ledger →" in page


class TestCadenceTickEndpoint:
    """Replan flow graft: the manual write cadence targets never had. G3 — it
    records a checkpoint, it never sets progress."""

    def test_plus_one_records_a_checkpoint_and_moves_the_count(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        tid = _cadence_goal(conn)
        page = client.get("/goals").text
        assert f'hx-post="/goals/targets/{tid}/tick"' in page

        response = client.post(f"/goals/targets/{tid}/tick")
        assert response.status_code == 200
        assert "1/3 this week" in response.text
        cp = conn.execute("SELECT * FROM checkpoint WHERE target_id = ?", (tid,)).fetchone()
        assert cp["source"] == "manual"
        assert cp["delta"] == 1

    def test_each_press_is_a_real_checkpoint(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """Deliberately NOT idempotent: two sessions done is two checkpoints, and an
        idempotent +1 would make the second one unrecordable."""
        tid = _cadence_goal(conn)
        client.post(f"/goals/targets/{tid}/tick")
        second = client.post(f"/goals/targets/{tid}/tick")
        assert "2/3 this week" in second.text
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM checkpoint WHERE target_id = ?", (tid,)
            ).fetchone()["n"]
            == 2
        )

    def test_unknown_and_non_cadence_targets_404(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        assert client.post("/goals/targets/9999/tick").status_code == 404
        _cadence_goal(conn)
        gid = conn.execute("SELECT id FROM goal").fetchone()["id"]
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, total_count, created_at)"
            " VALUES (?, 'total', 'Shadowing hours', 60, '2026-07-01T00:00:00Z')",
            (gid,),
        )
        conn.commit()
        total = conn.execute(
            "SELECT id FROM target WHERE kind = 'total'"
        ).fetchone()["id"]
        assert client.post(f"/goals/targets/{total}/tick").status_code == 404

    def test_inactive_target_is_not_tickable(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        tid = _cadence_goal(conn)
        conn.execute("UPDATE target SET active = 0 WHERE id = ?", (tid,))
        conn.commit()
        assert client.post(f"/goals/targets/{tid}/tick").status_code == 404


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
        # A day distinct from `saturday` above — plain `today - 1 day` collides
        # with it whenever the suite runs on a Sunday (yesterday *is* the most
        # recent Saturday then), which would assert two different labels for
        # the same heatmap cell. The day before `saturday` is always distinct
        # and always inside the heatmap's multi-week window.
        other_day = saturday - timedelta(days=1)
        chk.tick(conn, retired, other_day)
        conn.execute("UPDATE checklist_item SET active = 0 WHERE id = ?", (retired,))
        conn.commit()

        page = client.get("/goals").text
        # Saturday: 0 scheduled, 1 ticked → 1/1, never 1/0.
        assert f'aria-label="1/1 · {saturday.strftime("%d %b")}"' in page
        # other_day: the retired item's tick is gone from the numerator.
        assert f'aria-label="0/1 · {other_day.strftime("%d %b")}"' in page

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
        # The block names its event to the pointer and to a screen reader, and to
        # nothing else: a 9px ellipsized title on a 96px column is not information.
        assert 'title="09:00–10:30 Finish deck"' in page
        assert 'aria-label="09:00–10:30 Finish deck"' in page
        assert ">Finish deck<" not in page

    def test_week_blocks_carry_no_visible_text(
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
        blocks = re.findall(r'<a class="wev[^>]*>(.*?)</a>', page, re.S)
        assert blocks, "expected at least one week block"
        assert all(b.strip() == "" for b in blocks), blocks

    def test_capacity_band_gives_every_day_a_cell(
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
        # Format audit: one set of day headers — capacity lives in the grid
        # header cells, and the standalone band renders only on an empty week.
        assert 'class="wband"' not in page
        assert page.count('class="whd') == 7
        # Free hours are the headline number; 375 − 330 = 45 minutes.
        assert '<span class="wbn">0h45</span>' in page
        assert page.count('<span class="wbn none">—</span>') == 6  # unplanned days
        # Overflow is the one chip: gold, count only, inside the cell that owns it.
        assert "2 over" in page

    def test_empty_week_is_a_sentence_not_a_framed_void(
        self, client: TestClient
    ) -> None:
        page = client.get("/schedule/week?start=2026-07-27").text
        # No plans at all: the capacity strip and one sentence, never a 12-hour
        # empty grid.
        assert 'class="wk7"' not in page
        assert 'class="wband"' in page
        assert "Nothing planned this week" in page

    def test_week_verdict_speaks_only_when_the_week_is_over_committed(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO day_plan (local_date, tz, capacity_minutes, planned_minutes,"
            " generated_at, status) VALUES ('2026-07-28', 'America/Phoenix', 375, 330,"
            " '2026-07-28T05:50:00', 'accepted')"
        )
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title)"
            " VALUES (1, '2026-07-28T09:00:00-07:00', '2026-07-28T10:00:00-07:00',"
            " 'work', 'Finish deck')"
        )
        conn.commit()
        assert "planned against" not in client.get("/schedule/week?start=2026-07-27").text
        conn.execute("UPDATE day_plan SET planned_minutes = 500")
        conn.commit()
        assert (
            "8h planned against 6h available this week"
            in client.get("/schedule/week?start=2026-07-27").text
        )


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


class TestWhenLabel:
    """A plan has a date, not a deadline.

    `due_label` prefixes everything with "due", which reads as an obligation to hand
    something in — the exact distinction the engagement record exists to draw, so the
    profile page must not borrow it.
    """

    def test_a_plan_date_never_says_due(self) -> None:
        from datetime import date as _date

        from backglass.web.panels import when_label

        today = _date(2026, 7, 30)
        assert "due" not in when_label("2026-08-01", today)
        assert when_label("2026-07-30", today) == "today"
        assert when_label("2026-08-01", today) == "Sat"
        assert when_label("2026-09-15", today) == "15 Sep"
        assert when_label("2027-03-01", today) == "01 Mar 2027"
        assert when_label(None, today) == "no date yet"


class TestReviewQueueHoldsBothRecords:
    def _plan(self, conn: sqlite3.Connection, *, confidence: float, what: str) -> int:
        conn.execute(
            "INSERT INTO source_item (source, external_id, fetched_at, occurred_at,"
            " author, title, body_text, content_hash, triage_verdict)"
            " VALUES ('imessage', ?, '2026-07-20T09:00:00-07:00',"
            " '2026-07-20T09:00:00-07:00', 'Priya', 'msg', 'b', ?, 'keep')",
            (f"rev-{what}", f"revhash-{what}"),
        )
        source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
            " when_is_explicit, location, status, confidence, source_item_id, created_at)"
            " VALUES (1, 'social', ?, '2099-08-05', NULL, 1, NULL, 'proposed', ?, ?,"
            " '2026-07-20T09:00:00-07:00')",
            (what, confidence, source_id),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def test_a_low_confidence_plan_appears_in_the_dashboard_queue(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """The brief got plans in its review section and the dashboard panel did not, so
        the panel — and the "N extractions awaiting review" nudge counted off it —
        silently undercounted by every plan in the queue."""
        self._plan(conn, confidence=0.3, what="shaky plan")
        body = client.get("/").text
        review = panel_slice(body, "panel-review")
        assert "shaky plan" in review
        assert "Is this a real plan" in review

    def test_accepting_a_plan_believes_it(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        engagement_id = self._plan(conn, confidence=0.3, what="shaky plan")
        response = client.post(f"/review/plan/{engagement_id}/accept")
        assert response.status_code == 200
        row = conn.execute(
            "SELECT confidence FROM engagement WHERE id = ?", (engagement_id,)
        ).fetchone()
        assert float(row["confidence"]) == 1.0
        assert "shaky plan" not in panel_slice(client.get("/").text, "panel-review")

    def test_rejecting_a_plan_declines_it_so_re_extraction_cannot_revive_it(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """docs/11 §4 asks a rejection to tombstone the row. `declined` already means
        that and is already what the dedup pass can see, so no new column is needed."""
        engagement_id = self._plan(conn, confidence=0.3, what="shaky plan")
        assert client.post(f"/review/plan/{engagement_id}/reject").status_code == 200
        row = conn.execute(
            "SELECT status FROM engagement WHERE id = ?", (engagement_id,)
        ).fetchone()
        assert row["status"] == "declined"
