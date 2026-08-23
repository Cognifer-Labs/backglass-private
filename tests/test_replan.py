"""Dynamic replanning: proposed plans follow the day, accepted plans get a knock.

The fingerprint is the load-bearing piece: clock-free (a 05:45 plan and a 14:00
recomputation of the same world hash identically, or every afternoon sync reports
fake drift), and sensitive to exactly what a sync can change — the pool, the fixed
events, the window, the lanes.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backglass import notify
from backglass.config import Settings
from backglass.facts import remember
from backglass.ledger import USER_ID
from backglass.plan import planner, replan

PHOENIX = ZoneInfo("America/Phoenix")
#: A Tuesday, inside the default working days.
DAY = datetime(2026, 8, 18, 9, 0, tzinfo=PHOENIX)


@pytest.fixture(autouse=True)
def no_banners(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "_deliver", lambda *_a: "osascript")


def _commitment(conn: sqlite3.Connection, what: str, *, minutes: int = 45) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'manual', ?, '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z', ?, ?,"
        " ?, 'keep')",
        (USER_ID, f"x-{what}", what, what, f"h-{what}"),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " estimated_minutes, estimate_source, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, 0.9, 'open', ?, 'manual', ?, '2026-08-10T00:00:00Z')",
        (USER_ID, what, minutes, sid),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _plan(conn: sqlite3.Connection, settings: Settings, *, at: datetime = DAY) -> int:
    proposal = planner.propose(conn, settings, at.date())
    return planner.persist(conn, settings, proposal)


class TestTheFingerprint:
    def test_the_same_world_hashes_the_same_whatever_the_clock(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "alpha")
        first = planner.inputs_fingerprint(conn, settings, DAY.date())
        second = planner.inputs_fingerprint(conn, settings, DAY.date())
        assert first == second
        # And propose() at two different clocks stamps the same fingerprint.
        morning = planner.propose(conn, settings, DAY.date(), now=DAY)
        evening = planner.propose(
            conn, settings, DAY.date(), now=DAY.replace(hour=16)
        )
        assert morning.fingerprint == evening.fingerprint == first

    def test_a_new_commitment_changes_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "alpha")
        before = planner.inputs_fingerprint(conn, settings, DAY.date())
        _commitment(conn, "beta")
        assert planner.inputs_fingerprint(conn, settings, DAY.date()) != before

    def test_a_preference_change_changes_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "alpha")
        before = planner.inputs_fingerprint(conn, settings, DAY.date())
        remember(conn, settings, "preferences", "planner.priority", "school: alpha")
        assert planner.inputs_fingerprint(conn, settings, DAY.date()) != before

    def test_persist_stores_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "alpha")
        plan_id = _plan(conn, settings)
        row = conn.execute(
            "SELECT inputs_fingerprint FROM day_plan WHERE id = ?", (plan_id,)
        ).fetchone()
        assert row["inputs_fingerprint"] == planner.inputs_fingerprint(
            conn, settings, DAY.date()
        )


class TestTheBoundary:
    def test_an_unchanged_world_replans_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "alpha")
        _plan(conn, settings)
        assert replan.run(conn, settings, now=DAY) is None
        assert conn.execute("SELECT COUNT(*) AS n FROM day_plan").fetchone()["n"] == 1

    def test_a_rebuild_that_schedules_nothing_says_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The 17:15 case arriving through this path instead of the plan job.

        `persist` declines to replace a plan that scheduled work with one that schedules
        none, so a drift detected after the day is over writes nothing — and a
        "Today's plan was updated" banner for a plan that was not updated is a claim with
        nothing behind it. Rule 1 does not have an exemption for notifications.
        """
        _commitment(conn, "alpha")
        standing = _plan(conn, settings)
        _commitment(conn, "beta")  # the world moves

        assert replan.run(conn, settings, now=DAY.replace(hour=23, minute=50)) is None
        live = conn.execute(
            "SELECT id FROM day_plan WHERE status != 'superseded'"
        ).fetchone()
        assert int(live["id"]) == standing
        assert conn.execute("SELECT COUNT(*) AS n FROM notification").fetchone()["n"] == 0

    def test_a_proposed_plan_is_regenerated_when_the_world_moves(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "alpha")
        old_id = _plan(conn, settings)
        _commitment(conn, "beta")  # the world moves

        out = replan.run(conn, settings, now=DAY)

        assert out is not None and out.action == "replaced"
        old = conn.execute("SELECT status FROM day_plan WHERE id = ?", (old_id,)).fetchone()
        assert old["status"] == "superseded"
        live = conn.execute(
            "SELECT inputs_fingerprint FROM day_plan WHERE status != 'superseded'"
        ).fetchone()
        assert live["inputs_fingerprint"] == planner.inputs_fingerprint(
            conn, settings, DAY.date()
        )
        note = conn.execute("SELECT kind FROM notification").fetchone()
        assert note["kind"] == "plan-replaced"
        # And the replacement is stable: the next sync sees no drift.
        assert replan.run(conn, settings, now=DAY.replace(hour=10)) is None

    def test_an_accepted_plan_gets_a_knock_and_stands(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "alpha")
        plan_id = _plan(conn, settings)
        conn.execute(
            "UPDATE day_plan SET status = 'accepted',"
            " accepted_at = '2026-08-18T08:00:00-07:00' WHERE id = ?",
            (plan_id,),
        )
        _commitment(conn, "beta")

        out = replan.run(conn, settings, now=DAY)

        assert out is not None and out.action == "drifted"
        row = conn.execute("SELECT status FROM day_plan WHERE id = ?", (plan_id,)).fetchone()
        assert row["status"] == "accepted"  # never clobbered
        assert conn.execute("SELECT COUNT(*) AS n FROM day_plan").fetchone()["n"] == 1
        note = conn.execute("SELECT kind FROM notification").fetchone()
        assert note["kind"] == "plan-drift"

    def test_a_missing_plan_is_catchups_job_not_ours(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "alpha")
        assert replan.run(conn, settings, now=DAY) is None
        assert conn.execute("SELECT COUNT(*) AS n FROM day_plan").fetchone()["n"] == 0

    def test_a_pre_fingerprint_plan_is_never_treated_as_drifted(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Migration day must not replace every standing plan."""
        conn.execute(
            "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes,"
            " generated_at) VALUES (?, '2026-08-18', 'America/Phoenix', 240,"
            " '2026-08-18T05:45:00-07:00')",
            (USER_ID,),
        )
        _commitment(conn, "alpha")
        assert replan.run(conn, settings, now=DAY) is None

    def test_the_drift_knock_is_once_per_day(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "alpha")
        plan_id = _plan(conn, settings)
        conn.execute("UPDATE day_plan SET status = 'accepted' WHERE id = ?", (plan_id,))
        _commitment(conn, "beta")

        replan.run(conn, settings, now=DAY)
        replan.run(conn, settings, now=DAY.replace(hour=11))

        assert conn.execute("SELECT COUNT(*) AS n FROM notification").fetchone()["n"] == 1
