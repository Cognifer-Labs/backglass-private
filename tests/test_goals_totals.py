"""Total targets — the lifetime accumulator (Phase 10).

The medical-application ledger is the motivating instance: AMCAS-category hour
totals on the medical roadmap, each logged entry a checkpoint whose delta carries
the amount and whose note carries the org/supervisor. The mechanism is generic:
any goal can hold a total ("Ten users who come back").
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.goals import checkpoints, health
from backglass.goals import targets as targets_mod
from backglass.roadmap import instantiate, presets
from backglass.web.app import create_app

TODAY = date(2026, 7, 30)


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings))


def _goal(conn: sqlite3.Connection, *, target_date: str | None = "2027-06-01") -> int:
    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, target_date, definition_of_done,"
        " status, created_at) VALUES (1, 'Medical school application', 'annual', ?,"
        " 'Application submitted with every category at target', 'active',"
        " '2026-07-01T00:00:00Z')",
        (target_date,),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestSchema:
    def test_total_count_column_exists_and_migration_is_idempotent(
        self, conn: sqlite3.Connection
    ) -> None:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(target)")}
        assert "total_count" in cols
        # conftest's migrate() already ran; a second pass writes nothing new.
        from backglass.db import migrate

        migrate(conn)
        assert "total_count" in {r["name"] for r in conn.execute("PRAGMA table_info(target)")}


class TestEngine:
    def test_progress_sums_lifetime_not_week(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        goal_id = _goal(conn)
        tid = instantiate.add_total(conn, goal_id, "Shadowing hours", 60)
        # One entry three weeks back, one this week — a weekly view would drop the first.
        checkpoints.record(
            conn, tid, source="manual", delta=8,
            occurred_at=(TODAY - timedelta(days=21)).isoformat(),
        )
        checkpoints.record(
            conn, tid, source="manual", delta=3, occurred_at=TODAY.isoformat()
        )
        conn.commit()
        row = next(
            t for t in targets_mod.progress(conn, settings, TODAY) if t.target_id == tid
        )
        assert row.kind == "total"
        assert row.lifetime_done == 11
        assert row.total_count == 60
        assert not row.complete
        # Totals carry no standing weekly load — the capacity check must ignore them.
        assert row.weekly_minutes == 0

    def test_complete_at_threshold(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        goal_id = _goal(conn)
        tid = instantiate.add_total(conn, goal_id, "Shadowing hours", 10)
        checkpoints.record(conn, tid, source="manual", delta=10)
        conn.commit()
        row = next(
            t for t in targets_mod.progress(conn, settings, TODAY) if t.target_id == tid
        )
        assert row.complete

    def test_behind_pace_total_puts_the_goal_at_risk(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        goal_id = _goal(conn, target_date=(TODAY + timedelta(days=28)).isoformat())
        tid = instantiate.add_total(conn, goal_id, "Clinical hours", 200)
        # 1/week observed against 200 needed in 4 weeks: projected far past target.
        checkpoints.record(
            conn, tid, source="manual", delta=1,
            occurred_at=(TODAY - timedelta(days=7)).isoformat(),
        )
        conn.commit()
        r = next(x for x in health.risk(conn, settings, TODAY) if x.goal_id == goal_id)
        assert r.at_risk
        assert r.projected_completion is not None
        assert r.projected_completion > r.target_date  # type: ignore[operator]

    def test_finished_totals_do_not_flag_risk(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        goal_id = _goal(conn, target_date=(TODAY + timedelta(days=28)).isoformat())
        tid = instantiate.add_total(conn, goal_id, "Shadowing hours", 10)
        checkpoints.record(conn, tid, source="manual", delta=10)
        conn.commit()
        r = next(x for x in health.risk(conn, settings, TODAY) if x.goal_id == goal_id)
        assert not r.at_risk

    def test_staleness_never_goes_negative(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A checkpoint stored with tomorrow's UTC date must read as 0 days quiet,
        not -1 — an impossible claim on a provenance surface."""
        goal_id = _goal(conn)
        tid = instantiate.add_total(conn, goal_id, "Shadowing hours", 60)
        checkpoints.record(
            conn, tid, source="manual", delta=1,
            occurred_at=(TODAY + timedelta(days=1)).isoformat(),
        )
        conn.commit()
        s = next(
            x for x in health.staleness(conn, settings, TODAY) if x.goal_id == goal_id
        )
        assert s.days_quiet == 0
        assert s.chip() == "0 days quiet"

    def test_add_total_rejects_nonpositive(self, conn: sqlite3.Connection) -> None:
        goal_id = _goal(conn)
        with pytest.raises(ValueError):
            instantiate.add_total(conn, goal_id, "Hours", 0)


class TestMedicalPreset:
    def test_v2_carries_the_amcas_totals(self) -> None:
        preset = presets.load("medical")
        assert preset.version == "2"
        assert [t.key for t in preset.totals] == [
            "shadowing", "clinical", "volunteering", "research", "leadership",
        ]
        assert all(t.total_count > 0 for t in preset.totals)

    def test_instantiate_creates_total_targets(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        rid = instantiate.instantiate(conn, settings, presets.load("medical"), TODAY)
        goal_id = conn.execute(
            "SELECT goal_id FROM roadmap WHERE id = ?", (rid,)
        ).fetchone()["goal_id"]
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM target WHERE goal_id = ? ORDER BY id", (goal_id,)
            )
        ]
        assert kinds.count("total") == 5
        assert kinds.count("cadence") == 2
        assert kinds.count("milestone") == 8
        stamp = conn.execute(
            "SELECT path_version FROM roadmap WHERE id = ?", (rid,)
        ).fetchone()["path_version"]
        assert stamp == "2"


class TestRoadmapPage:
    def _start_medical(self, client: TestClient, conn: sqlite3.Connection) -> tuple[int, int]:
        client.post("/roadmaps/start/medical", follow_redirects=True)
        rid = conn.execute("SELECT id FROM roadmap").fetchone()["id"]
        tid = conn.execute(
            "SELECT t.id FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
            "WHERE r.id = ? AND t.kind = 'total' ORDER BY t.id LIMIT 1",
            (rid,),
        ).fetchone()["id"]
        return rid, tid

    def test_page_shows_progress_header_and_totals(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, _ = self._start_medical(client, conn)
        page = client.get(f"/roadmaps/{rid}").text
        assert 'id="roadmap-totals"' in page
        assert "0/8 steps" in page
        # The next pending step is marked in the timetable now, not summarized in
        # the masthead: a 4px black left rule plus a NEXT label on the row itself.
        assert "nextstep" in page
        assert '<span class="nextlbl">Next</span>' in page
        assert "Shadowing hours" in page
        assert "0/60 · 0%" in page

    def test_log_writes_a_checkpoint_with_delta_and_note(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        response = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log",
            data={"amount": "3", "note": "Banner ER · Dr. Rao"},
        )
        assert response.status_code == 200
        assert "3/60" in response.text
        assert "Banner ER · Dr. Rao" in response.text
        cp = conn.execute("SELECT * FROM checkpoint WHERE target_id = ?", (tid,)).fetchone()
        assert cp["delta"] == 3
        assert cp["source"] == "manual"
        assert cp["note"] == "Banner ER · Dr. Rao"

    def test_set_changes_the_bar_not_the_hours(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        client.post(f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "6", "note": ""})
        response = client.post(f"/roadmaps/{rid}/totals/{tid}/set", data={"total": "80"})
        assert response.status_code == 200
        assert "6/80" in response.text

    def test_rejects_bad_writes(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        assert (
            client.post(
                f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "0", "note": ""}
            ).status_code
            == 422
        )
        # A milestone target is not a total — no silent write to the wrong number.
        milestone = conn.execute(
            "SELECT t.id FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
            "WHERE r.id = ? AND t.kind = 'milestone' LIMIT 1",
            (rid,),
        ).fetchone()["id"]
        assert (
            client.post(
                f"/roadmaps/{rid}/totals/{milestone}/log", data={"amount": "1", "note": ""}
            ).status_code
            == 404
        )

    def test_goal_card_echoes_the_total(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        client.post(f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "3", "note": ""})
        page = client.get("/goals").text
        assert "3/60 logged" in page


class TestGoalsPageLogging:
    """Phase 11: totals on goals with no roadmap are loggable from the Goals page."""

    def _total(self, conn: sqlite3.Connection) -> int:
        gid = _goal(conn)
        tid = instantiate.add_total(conn, gid, "Founder discovery calls", 5)
        conn.commit()
        return tid

    def test_log_from_goals_page(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        tid = self._total(conn)
        page = client.get("/goals").text
        assert f"/goals/targets/{tid}/log" in page
        response = client.post(
            f"/goals/targets/{tid}/log", data={"amount": "2", "note": "YC office hours"}
        )
        assert response.status_code == 200
        assert "2/5 logged" in response.text
        cp = conn.execute("SELECT * FROM checkpoint WHERE target_id = ?", (tid,)).fetchone()
        assert cp["delta"] == 2
        assert cp["note"] == "YC office hours"

    def test_rejects_non_totals_and_bad_amounts(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        gid = _goal(conn)
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at)"
            " VALUES (?, 'cadence', 'Sessions', 3, '2026-07-01T00:00:00Z')", (gid,)
        )
        cadence = conn.execute("SELECT id FROM target").fetchone()["id"]
        conn.commit()
        assert client.post(
            f"/goals/targets/{cadence}/log", data={"amount": "1", "note": ""}
        ).status_code == 404
        tid = instantiate.add_total(conn, gid, "Calls", 5)
        conn.commit()
        assert client.post(
            f"/goals/targets/{tid}/log", data={"amount": "0", "note": ""}
        ).status_code == 422


class TestLocalStamps:
    """Phase 11: manual provenance carries the owner's local date, not UTC's.

    Typed at 9pm in Phoenix, a claim must not read as tomorrow — the '-1d' bug class."""

    def _phx_today(self) -> str:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Phoenix")).date().isoformat()

    def test_quick_add_occurred_at_is_owner_local(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        client.post(
            "/commitments/quick-add",
            data={"what": "Send the deck", "direction": "i_owe",
                  "counterparty": "Dana", "due_at": "2026-08-05"},
        )
        row = conn.execute("SELECT occurred_at FROM source_item").fetchone()
        assert row["occurred_at"][:10] == self._phx_today()
        # And an explicit offset — P13's "stored UTC" satisfied by absolutes.
        assert "+" in row["occurred_at"] or "-07:00" in row["occurred_at"]

    def test_logged_hours_carry_owner_local_date(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        gid = _goal(conn)
        tid = instantiate.add_total(conn, gid, "Shadowing hours", 60)
        conn.commit()
        client.post(f"/goals/targets/{tid}/log", data={"amount": "1", "note": ""})
        cp = conn.execute("SELECT occurred_at FROM checkpoint").fetchone()
        assert cp["occurred_at"][:10] == self._phx_today()


class TestRoadmapEditsAndUnlog:
    """The owner's words and receipts are editable: rename the roadmap, a step,
    or a total, and take a wrongly-entered log back out (G10 — progress is
    summed on read, so removal recomputes by construction)."""

    def _start_medical(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> tuple[int, int]:
        client.post("/roadmaps/start/medical", follow_redirects=True)
        rid = conn.execute("SELECT id FROM roadmap").fetchone()["id"]
        tid = conn.execute(
            "SELECT t.id FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
            "WHERE r.id = ? AND t.kind = 'total' ORDER BY t.id LIMIT 1",
            (rid,),
        ).fetchone()["id"]
        return rid, tid

    def test_unlog_removes_the_entry_and_the_bar_recomputes(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        client.post(f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "6", "note": "typo"})
        cid = conn.execute("SELECT id FROM checkpoint").fetchone()["id"]
        fragment = client.post(f"/roadmaps/{rid}/totals/{tid}/unlog/{cid}").text
        assert conn.execute("SELECT COUNT(*) AS n FROM checkpoint").fetchone()["n"] == 0
        assert "0/60 · 0%" in fragment

    def test_unlog_refuses_a_checkpoint_of_another_target(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        other = conn.execute(
            "SELECT t.id FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
            "WHERE r.id = ? AND t.kind = 'total' AND t.id != ? ORDER BY t.id LIMIT 1",
            (rid, tid),
        ).fetchone()["id"]
        client.post(f"/roadmaps/{rid}/totals/{other}/log", data={"amount": "2", "note": ""})
        cid = conn.execute("SELECT id FROM checkpoint").fetchone()["id"]
        # Addressed through the wrong target: 404, and the receipt survives.
        assert client.post(f"/roadmaps/{rid}/totals/{tid}/unlog/{cid}").status_code == 404
        assert conn.execute("SELECT COUNT(*) AS n FROM checkpoint").fetchone()["n"] == 1

    def test_rename_total_changes_the_targets_title(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        response = client.post(
            f"/roadmaps/{rid}/totals/{tid}/title", data={"title": "Shadowing (Banner)"}
        )
        assert response.status_code == 200
        row = conn.execute("SELECT title FROM target WHERE id = ?", (tid,)).fetchone()
        assert row["title"] == "Shadowing (Banner)"
        assert (
            client.post(
                f"/roadmaps/{rid}/totals/{tid}/title", data={"title": "   "}
            ).status_code
            == 422
        )

    def test_step_edit_updates_step_and_syncs_its_milestone_target(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, _ = self._start_medical(client, conn)
        step = conn.execute(
            "SELECT id, target_id FROM roadmap_step WHERE roadmap_id = ? "
            "AND target_id IS NOT NULL ORDER BY sort_order LIMIT 1",
            (rid,),
        ).fetchone()
        response = client.post(
            f"/roadmaps/{rid}/steps/{step['id']}/edit",
            data={"title": "Retake MCAT", "detail": "aim 520"},
        )
        assert response.status_code == 200
        row = conn.execute(
            "SELECT title, detail FROM roadmap_step WHERE id = ?", (step["id"],)
        ).fetchone()
        assert (row["title"], row["detail"]) == ("Retake MCAT", "aim 520")
        # The milestone target was named after the step at instantiation; the
        # rename moves it too, so checkpoints stay filed under the shown name.
        target = conn.execute(
            "SELECT title FROM target WHERE id = ?", (step["target_id"],)
        ).fetchone()
        assert target["title"] == "Retake MCAT"
        assert (
            client.post(
                f"/roadmaps/{rid}/steps/{step['id']}/edit", data={"title": ""}
            ).status_code
            == 422
        )

    def test_roadmap_edit_renames_roadmap_and_goal_together(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, _ = self._start_medical(client, conn)
        response = client.post(
            f"/roadmaps/{rid}/edit",
            data={"title": "MD by 2034", "definition_of_done": "Matched."},
            follow_redirects=False,
        )
        assert response.status_code == 303
        roadmap = conn.execute(
            "SELECT title, goal_id FROM roadmap WHERE id = ?", (rid,)
        ).fetchone()
        goal = conn.execute(
            "SELECT title, definition_of_done FROM goal WHERE id = ?",
            (roadmap["goal_id"],),
        ).fetchone()
        assert roadmap["title"] == "MD by 2034"
        assert goal["title"] == "MD by 2034"
        assert goal["definition_of_done"] == "Matched."

    def test_steps_render_as_an_expandable_timeline(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, _ = self._start_medical(client, conn)
        page = client.get(f"/roadmaps/{rid}").text
        n_steps = conn.execute(
            "SELECT COUNT(*) AS n FROM roadmap_step WHERE roadmap_id = ?", (rid,)
        ).fetchone()["n"]
        assert page.count('<details class="stepx') == n_steps
        assert page.count('class="logform editform"') == n_steps
        assert 'class="atl"' in page

    def test_wrongly_logged_entry_shows_an_unlog_control(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        client.post(f"/roadmaps/{rid}/totals/{tid}/log", data={"amount": "4", "note": ""})
        cid = conn.execute("SELECT id FROM checkpoint").fetchone()["id"]
        page = client.get(f"/roadmaps/{rid}").text
        assert f"/roadmaps/{rid}/totals/{tid}/unlog/{cid}" in page
