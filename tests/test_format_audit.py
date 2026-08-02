"""Format-audit rulings (tasks/todo.md §Format audit), each asserted directly.

1. An alert is abnormal + actionable + names its subject; goal risk is status.
2. Vermilion means broken/overdue — never "young data".
3. The dashboard Goals panel is a per-goal summary, not a clone of /goals.
4. NEEDS ATTENTION is the worst two, not everyone with a blemish.
5. People ranks by need and splits service desks from humans.
6. Milestones are dates, not streams.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.web.app import create_app
from tests.conftest import panel_slice


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn  # migrated db on disk; the app opens its own connections
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _seed_goal(conn: sqlite3.Connection, title: str, target_kind: str = "cadence") -> int:
    cur = conn.execute(
        "INSERT INTO goal (user_id, title, horizon, target_date, definition_of_done,"
        " status, created_at) VALUES (1, ?, 'quarterly', '2026-09-30', 'Done means done',"
        " 'active', '2026-07-01T00:00:00Z')",
        (title,),
    )
    goal_id = int(cur.lastrowid or 0)
    if target_kind == "cadence":
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at)"
            " VALUES (?, 'cadence', ?, 3, '2026-07-01T00:00:00Z')",
            (goal_id, f"{title} cadence"),
        )
    else:
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, created_at)"
            " VALUES (?, 'milestone', ?, '2026-07-01T00:00:00Z')",
            (goal_id, f"{title} milestone"),
        )
    return goal_id


class TestAlertPolicy:
    def test_a_failing_source_alert_names_the_source(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        from backglass.connectors import credentials

        credentials.mark_failed(conn, "gmail:personal", "invalid_grant")
        body = client.get("/schedule").text
        assert "gmail:personal is failing — views are incomplete" in body

    def test_goal_risk_is_not_an_alert(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        # Two weeks of sustained risk used to append a gold alert per goal.
        # It is status now: chips and sentences on the goal surfaces only.
        _seed_goal(conn, "Ship the fundraise")
        conn.commit()
        body = client.get("/").text
        assert "at risk two weeks running" not in body

    def test_alerts_cap_at_four_and_count_the_overflow(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.connectors import credentials
        from backglass.web import panels

        for name in ("gmail:a", "gmail:b", "gmail:c", "gmail:d", "gmail:e"):
            credentials.mark_failed(conn, name, "invalid_grant")
        conn.commit()
        sb = panels.sidebar(conn, settings, date(2026, 8, 1))
        assert len(sb.alerts) == 4
        assert sb.more_alerts == 1


class TestYoungDataInk:
    def test_never_touched_person_is_new_not_cold(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name, role) VALUES"
            " (1, 'person', 'Aimee Munoz', 'Volunteer Services Assistant')"
        )
        conn.commit()
        levels = {t.name: t.level for t in touch.cold(conn, settings, date(2026, 8, 1))}
        assert levels["Aimee Munoz"] == "new"

    def test_new_person_renders_neutral_not_vermilion(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name, role) VALUES"
            " (1, 'person', 'Aimee Munoz', 'Volunteer Services Assistant')"
        )
        conn.commit()
        body = client.get("/people").text
        assert '<span class="chip k-plain">no interactions yet</span>' in body
        assert "k-verm" not in body

    def test_never_touched_person_is_not_going_cold(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name, role) VALUES"
            " (1, 'person', 'Aimee Munoz', 'Volunteer Services Assistant')"
        )
        conn.commit()
        body = client.get("/people?cold=true").text
        assert "Aimee Munoz" not in body

    def test_uncheckpointed_goal_chip_is_neutral_ink(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _seed_goal(conn, "Ship the fundraise")
        conn.commit()
        body = client.get("/").text
        assert '<span class="chip k-plain">no checkpoints yet</span>' in body


class TestDashboardGoalsSummary:
    def test_one_line_per_goal_and_no_controls(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _seed_goal(conn, "Ship the fundraise")
        conn.commit()
        body = client.get("/").text
        panel = panel_slice(body, "panel-goals")
        assert "0/3 this week" in panel
        # No cadence pickers, no logging on the glance surface.
        assert "/wk" not in panel
        assert "+1" not in panel
        assert 'href="/goals"' in panel


class TestNeedsAttentionCap:
    def test_flagged_is_the_worst_two(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.web.routes.goals import goal_cards

        for title in ("Alpha", "Beta", "Gamma"):
            _seed_goal(conn, title)
        conn.commit()
        cards = goal_cards(conn, settings, date(2026, 8, 1))
        assert len([c for c in cards.cards if c.flagged]) == 3
        assert len(cards.flagged) == 2
        # Nothing vanishes: the rest land under ON TRACK.
        assert len(cards.flagged) + len(cards.healthy) == 3


class TestPeopleGrouping:
    def test_service_desks_split_from_humans(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name, role) VALUES"
            " (1, 'person', 'Shawn Gathas', 'Guidance Coordinator')"
        )
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name) VALUES"
            " (1, 'person', 'ASU Transportation Portal')"
        )
        conn.commit()
        body = client.get("/people").text
        assert "Orgs &amp; services" in body
        # The org lands after the group header; the human before it.
        human_pos = body.index("Shawn Gathas")
        header_pos = body.index("Orgs &amp; services")
        org_pos = body.index("ASU Transportation Portal")
        assert human_pos < header_pos < org_pos

    def test_open_commitments_rank_first(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name)"
            " VALUES (1, 'person', 'Quiet Contact')"
        )
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name)"
            " VALUES (1, 'person', 'Busy Contact')"
        )
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, content_hash) VALUES (1, 'gmail:personal', 'x1',"
            " '2026-07-30T08:00:00Z', '2026-07-30T08:00:00Z', 'h1')"
        )
        conn.execute(
            "INSERT INTO commitment (user_id, source_item_id, direction, what,"
            " counterparty_entity_id, status, confidence, created_at) VALUES"
            " (1, 1, 'i_owe', 'Send the deck', 2, 'open', 0.99, '2026-07-30T08:05:00Z')"
        )
        conn.commit()
        body = client.get("/people").text
        assert body.index("Busy Contact") < body.index("Quiet Contact")

    def test_org_like_heuristic_and_tag_override(self) -> None:
        from backglass.people.profiles import org_like

        assert org_like({"canonical_name": "ASU Housing", "tags": []})
        assert org_like({"canonical_name": "Financial Aid Office", "tags": []})
        assert not org_like({"canonical_name": "college counselor", "tags": []})
        assert not org_like(
            {"canonical_name": "Shawn Gathas", "role": "Coordinator", "tags": []}
        )
        # The owner's tag beats the heuristic in both directions.
        assert org_like({"canonical_name": "Sallie Mae", "tags": ["org"]})
        assert not org_like({"canonical_name": "ASU Housing", "tags": ["person"]})
        # Persisted kind beats everything — a flipped row is decided.
        assert org_like({"canonical_name": "Sallie Mae", "kind": "org", "tags": []})

    def test_an_org_tag_writes_through_to_entity_kind(
        self, conn: sqlite3.Connection
    ) -> None:
        """The tag is the owner's classification, not just a display hint: tagging
        `org` flips entity.kind (the resolver matches both kinds, so nothing
        fragments), and tagging `person` flips it back."""
        from backglass.web import actions

        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name)"
            " VALUES (1, 'person', 'Sallie Mae')"
        )
        eid = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])

        actions.person_update(conn, eid, tags="org, lender")
        kind = conn.execute("SELECT kind FROM entity WHERE id = ?", (eid,)).fetchone()
        assert kind["kind"] == "org"

        actions.person_update(conn, eid, tags="person")
        kind = conn.execute("SELECT kind FROM entity WHERE id = ?", (eid,)).fetchone()
        assert kind["kind"] == "person"


class TestMilestoneList:
    def test_milestones_render_as_a_compact_list(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        _seed_goal(conn, "Get into med school", target_kind="milestone")
        conn.commit()
        body = client.get("/goals").text
        assert 'class="mlist"' in body
        assert "last —" not in body
