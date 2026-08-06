"""What Work & Activities would still be missing, said on the page that builds it.

`amcas-export` has always known: it prints "Organization: —", "Contact: — (add a
supervisor entity)", "no dates logged", and a zero-character draft. But it only says so
when it runs, and there is no reason to run it until the application is due — by which
point the four years it summarizes are over. `export_gaps` moves that checklist onto the
registry, and the binding test below is what stops the two from drifting apart.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from backglass import __main__ as cli
from backglass.config import Settings
from backglass.goals import activities, checkpoints
from backglass.web.app import create_app


def _total(conn: sqlite3.Connection, goal_id: int, title: str = "Clinical hours") -> int:
    conn.execute(
        "INSERT INTO target (user_id, goal_id, title, kind, total_count, active,"
        " created_at) VALUES (1, ?, ?, 'total', 150, 1, '2026-07-01T00:00:00Z')",
        (goal_id, title),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _goal(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, target_date, definition_of_done,"
        " status, created_at) VALUES (1, 'Medical school', 'annual', '2027-06-01',"
        " 'Submitted', 'active', '2026-07-01T00:00:00Z')"
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _row(conn: sqlite3.Connection, activity_id: int) -> dict[str, Any]:
    return next(a for a in activities.list_with_hours(conn) if a["id"] == activity_id)


class TestGaps:
    def test_a_bare_activity_is_missing_all_four(self, conn: sqlite3.Connection) -> None:
        aid = activities.add(conn, title="ED scribe", category="clinical")
        assert activities.export_gaps(_row(conn, aid)) == list(activities.EXPORT_FIELDS)

    def test_an_hour_with_a_note_closes_dates_and_notes_together(
        self, conn: sqlite3.Connection
    ) -> None:
        """One logged hour carrying words answers two of the four: the export falls
        back to the logged span for dates, and the note is the draft material."""
        goal = _goal(conn)
        tid = _total(conn, goal)
        aid = activities.add(conn, title="ED scribe", category="clinical")
        checkpoints.record(
            conn, tid, source="manual", delta=4, activity_id=aid, note="Banner ER · Dr. Rao"
        )
        assert activities.export_gaps(_row(conn, aid)) == ["org", "contact"]

    def test_an_hour_without_words_leaves_the_description_empty(
        self, conn: sqlite3.Connection
    ) -> None:
        goal = _goal(conn)
        tid = _total(conn, goal)
        aid = activities.add(conn, title="ED scribe", category="clinical")
        checkpoints.record(conn, tid, source="manual", delta=4, activity_id=aid)
        assert "notes" in activities.export_gaps(_row(conn, aid))
        assert "dates" not in activities.export_gaps(_row(conn, aid))

    def test_whitespace_is_not_an_organization(self, conn: sqlite3.Connection) -> None:
        """`add` strips to None, but a row edited by any other path can hold spaces —
        and " " printed as an organization is a placeholder wearing a value's clothes."""
        aid = activities.add(conn, title="ED scribe", category="clinical")
        conn.execute("UPDATE activity SET org = '   ' WHERE id = ?", (aid,))
        assert "org" in activities.export_gaps(_row(conn, aid))

    def test_a_complete_activity_needs_nothing(self, conn: sqlite3.Connection) -> None:
        goal = _goal(conn)
        tid = _total(conn, goal)
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name)"
            " VALUES (1, 'person', 'Dr. Rao')"
        )
        eid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        aid = activities.add(
            conn, title="ED scribe", org="Banner", role="Scribe", category="clinical",
            contact_entity_id=eid, started_on="2026-01-19",
        )
        checkpoints.record(conn, tid, source="manual", delta=4, activity_id=aid, note="ICU")
        assert activities.export_gaps(_row(conn, aid)) == []


class TestTheExportAgrees:
    """The anti-drift test. Every word the page prints under Needs must correspond to a
    placeholder the export actually emits, and an activity the page calls ready must
    produce an export section with none of them. Without this the two surfaces are one
    refactor away from disagreeing about the same record.
    """

    #: What the export prints in place of each field `export_gaps` names.
    PLACEHOLDERS = {
        "org": "Organization: —",
        "contact": "Contact: — (add a supervisor entity)",
        "dates": "no dates logged",
        "notes": "Draft material: 0 chars",
    }

    @pytest.fixture
    def cli_settings(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> Settings:
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        return settings

    def test_every_gap_word_is_a_placeholder_the_export_prints(
        self, conn: sqlite3.Connection, cli_settings: Settings
    ) -> None:
        aid = activities.add(conn, title="ED scribe", category="clinical")
        conn.commit()
        gaps = activities.export_gaps(_row(conn, aid))
        assert set(gaps) == set(self.PLACEHOLDERS), "a gap word with no export placeholder"
        result = CliRunner().invoke(cli.app, ["amcas-export"])
        assert result.exit_code == 0, result.output
        for gap in gaps:
            assert self.PLACEHOLDERS[gap] in result.output, gap

    def test_a_ready_activity_exports_with_no_placeholder(
        self, conn: sqlite3.Connection, cli_settings: Settings
    ) -> None:
        goal = _goal(conn)
        tid = _total(conn, goal)
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name)"
            " VALUES (1, 'person', 'Dr. Rao')"
        )
        eid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        aid = activities.add(
            conn, title="ED scribe", org="Banner", role="Scribe", category="clinical",
            contact_entity_id=eid, started_on="2026-01-19",
        )
        checkpoints.record(conn, tid, source="manual", delta=4, activity_id=aid, note="ICU")
        conn.commit()
        assert activities.export_gaps(_row(conn, aid)) == []
        result = CliRunner().invoke(cli.app, ["amcas-export"])
        assert result.exit_code == 0, result.output
        for placeholder in self.PLACEHOLDERS.values():
            assert placeholder not in result.output


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
            "WHERE r.id = ? AND t.kind = 'total' AND t.title LIKE 'Clinical%'",
            (rid,),
        ).fetchone()["id"]
        return rid, tid

    def test_the_registry_says_what_each_activity_still_needs(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        client.post(f"/roadmaps/{rid}/activities",
                    data={"title": "ED scribe", "category": "clinical"})
        aid = conn.execute("SELECT id FROM activity").fetchone()["id"]
        # Asserted on the write's own body: #roadmap-totals is swapped whole, so a
        # column built anywhere but the shared context builder vanishes on first log.
        page = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log",
            data={"amount": "4", "note": "Dr. Rao", "activity_id": str(aid)},
        ).text
        assert ">Needs<" in page
        # The note and the logged date closed two of the four; org and contact remain.
        assert "org · contact" in page
        assert "0 of 1 ready" in page

    def test_a_complete_activity_reads_ready(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        rid, tid = self._start_medical(client, conn)
        client.post(f"/roadmaps/{rid}/activities",
                    data={"title": "ED scribe", "org": "Banner", "role": "Scribe",
                          "category": "clinical"})
        aid = conn.execute("SELECT id FROM activity").fetchone()["id"]
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name)"
            " VALUES (1, 'person', 'Dr. Rao')"
        )
        eid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute("UPDATE activity SET contact_entity_id = ? WHERE id = ?", (eid, aid))
        conn.commit()
        page = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log",
            data={"amount": "4", "note": "ICU", "activity_id": str(aid)},
        ).text
        assert ">ready<" in page
        assert "1 of 1 ready" in page
