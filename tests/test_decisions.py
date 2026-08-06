"""Major decisions: supersession, revisit, the commitment link, and the page."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backglass import decisions
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.web.app import create_app


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def a_commitment(conn: sqlite3.Connection, what: str, *, status: str = "open") -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'gmail:personal', ?, ?, '2026-07-14T09:15:00-07:00',"
        " 'Dana <dana@example.gov>', 'Plan', 'b', '{}', ?, 'keep')",
        (USER_ID, f"m-{what}", now_iso(), f"h-{what}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " source_item_id, created_at) VALUES (?, 'i_owe', ?, 0.9, ?, ?, ?)",
        (USER_ID, what, status, source_id, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def commitment_status(conn: sqlite3.Connection, commitment_id: int) -> str:
    row = conn.execute(
        "SELECT status FROM commitment WHERE id = ?", (commitment_id,)
    ).fetchone()
    return str(row["status"])


class TestEngine:
    def test_record_and_list(self, conn: sqlite3.Connection, settings: Settings) -> None:
        decisions.record(
            conn, settings, "Sallie Mae application", "not doing it",
            reasoning="federal loans cover the year",
        )
        conn.commit()
        [d] = decisions.active(conn)
        assert d.title == "Sallie Mae application"
        assert d.choice == "not doing it"
        assert d.reasoning == "federal loans cover the year"
        assert d.commitment_id is None

    def test_same_title_supersedes_case_insensitively(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        first, _ = decisions.record(conn, settings, "Sallie Mae application", "not doing it")
        second, _ = decisions.record(
            conn, settings, "  sallie mae  APPLICATION ", "doing it after all"
        )
        conn.commit()
        [d] = decisions.active(conn)
        assert d.choice == "doing it after all"
        old = conn.execute("SELECT * FROM decision WHERE id = ?", (first,)).fetchone()
        assert old["status"] == "superseded"
        assert old["superseded_by"] == second
        assert old["choice"] == "not doing it"  # history intact

    def test_different_titles_coexist(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        decisions.record(conn, settings, "Sallie Mae application", "not doing it")
        decisions.record(conn, settings, "Early Start arrival", "declined")
        conn.commit()
        assert len(decisions.active(conn)) == 2

    def test_recording_closes_the_open_commitment(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        cid = a_commitment(conn, "Apply to Sallie Mae")
        did, closed = decisions.record(
            conn, settings, "Sallie Mae application", "not doing it", commitment_id=cid
        )
        conn.commit()
        assert closed is True
        assert commitment_status(conn, cid) == "dropped"
        note = conn.execute(
            "SELECT resolution_note FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["resolution_note"]
        assert f"decision:{did}" in note  # the drop says which decision settled it
        [d] = decisions.active(conn)
        assert d.commitment_title == "Apply to Sallie Mae"

    def test_a_closed_commitment_is_linked_but_untouched(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        cid = a_commitment(conn, "Apply to Sallie Mae", status="done")
        _, closed = decisions.record(
            conn, settings, "Sallie Mae application", "not doing it", commitment_id=cid
        )
        conn.commit()
        assert closed is False
        assert commitment_status(conn, cid) == "done"
        # And the list says so: linked is "re:", not "closed:" — a decision must not
        # claim an act (the drop) that never happened.
        [d] = decisions.active(conn)
        assert d.closed_commitment is False

    def test_record_is_one_transaction(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """An interrupt mid-act must leave nothing: no standing decision whose
        commitment is still open, no second active row for a superseded title.
        The trigger aborts the final write (the commitment drop); with the
        BEGIN/ROLLBACK wrapper every earlier write vanishes with it."""
        prior, _ = decisions.record(conn, settings, "Sallie Mae application", "undecided")
        cid = a_commitment(conn, "Apply to Sallie Mae")
        conn.execute(
            "CREATE TEMP TRIGGER boom BEFORE UPDATE ON commitment"
            " BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        with pytest.raises(sqlite3.DatabaseError):
            decisions.record(
                conn, settings, "Sallie Mae application", "not doing it",
                commitment_id=cid,
            )
        conn.execute("DROP TRIGGER boom")
        assert commitment_status(conn, cid) == "open"
        [d] = decisions.active(conn)  # the prior decision alone, still active
        assert d.decision_id == prior
        assert d.choice == "undecided"

    def test_unknown_commitment_refuses(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        with pytest.raises(decisions.DecisionError):
            decisions.record(conn, settings, "t", "c", commitment_id=99999)
        # The refusal writes nothing — no half-recorded decision.
        assert decisions.active(conn) == []

    def test_revisit_retracts_without_reopening(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        cid = a_commitment(conn, "Apply to Sallie Mae")
        did, _ = decisions.record(
            conn, settings, "Sallie Mae application", "not doing it", commitment_id=cid
        )
        decisions.revisit(conn, did)
        conn.commit()
        assert decisions.active(conn) == []
        row = conn.execute("SELECT status FROM decision WHERE id = ?", (did,)).fetchone()
        assert row["status"] == "retracted"
        # The commitment the decision closed stays closed — no resurrection by side effect.
        assert commitment_status(conn, cid) == "dropped"
        with pytest.raises(decisions.DecisionError):
            decisions.revisit(conn, did)  # already inactive

    def test_rejects_blank(self, conn: sqlite3.Connection, settings: Settings) -> None:
        with pytest.raises(decisions.DecisionError):
            decisions.record(conn, settings, "  ", "choice")
        with pytest.raises(decisions.DecisionError):
            decisions.record(conn, settings, "title", "")


class TestDecisionsPage:
    def test_renders_empty_then_record_then_revisit(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        page = client.get("/decisions")
        assert page.status_code == 200
        assert "No decisions recorded yet" in page.text

        added = client.post(
            "/decisions",
            data={"title": "Sallie Mae application", "choice": "not doing it",
                  "reasoning": "federal loans cover the year", "commitment": ""},
        )
        assert added.status_code == 200
        assert "Sallie Mae application" in added.text
        assert "not doing it" in added.text
        assert "federal loans cover the year" in added.text

        did = conn.execute("SELECT id FROM decision WHERE status='active'").fetchone()["id"]
        revisited = client.post(f"/decisions/{did}/revisit")
        assert revisited.status_code == 200
        assert "federal loans cover the year" not in revisited.text
        assert client.post("/decisions/99999/revisit").status_code == 404

    def test_recording_through_the_form_closes_the_commitment(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = a_commitment(conn, "Apply to Sallie Mae")
        conn.commit()  # the app reads through its own connection
        page = client.get("/decisions")
        assert "Apply to Sallie Mae" in page.text  # offered in the also-closes select

        added = client.post(
            "/decisions",
            data={"title": "Sallie Mae application", "choice": "not doing it",
                  "reasoning": "", "commitment": str(cid)},
        )
        assert added.status_code == 200
        assert "closed: Apply to Sallie Mae" in added.text
        assert commitment_status(conn, cid) == "dropped"

    def test_blank_decision_is_422(self, client: TestClient) -> None:
        response = client.post(
            "/decisions", data={"title": " ", "choice": "c", "reasoning": "", "commitment": ""}
        )
        assert response.status_code == 422
        bad_id = client.post(
            "/decisions",
            data={"title": "t", "choice": "c", "reasoning": "", "commitment": "not-a-number"},
        )
        assert bad_id.status_code == 422

    def test_nav_carries_decisions_on_every_page(self, client: TestClient) -> None:
        for path in ("/", "/memory", "/decisions"):
            assert "Decisions" in client.get(path).text, path

    def test_linked_but_not_closed_reads_re_not_closed(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = a_commitment(conn, "Apply to Sallie Mae", status="done")
        conn.commit()
        added = client.post(
            "/decisions",
            data={"title": "Sallie Mae application", "choice": "not doing it",
                  "reasoning": "", "commitment": str(cid)},
        )
        assert "re: Apply to Sallie Mae" in added.text
        assert "closed: Apply to Sallie Mae" not in added.text


class TestCli:
    """The second door. A guard on one door is not a guard — same rules as the page."""

    @pytest.fixture(autouse=True)
    def cli_settings(
        self, settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del conn  # the CLI opens its own connection against the same migrated file
        from backglass import __main__ as cli

        monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def test_record_list_revisit(self, conn: sqlite3.Connection) -> None:
        from typer.testing import CliRunner

        from backglass import __main__ as cli

        runner = CliRunner()
        empty = runner.invoke(cli.app, ["decisions"])
        assert empty.exit_code == 0, empty.output
        assert "no decisions recorded yet" in empty.output

        cid = a_commitment(conn, "Apply to Sallie Mae")
        conn.commit()
        made = runner.invoke(
            cli.app,
            ["decisions", "record", "Sallie Mae application", "not doing it",
             "--why", "federal loans cover the year", "--closes", str(cid)],
        )
        assert made.exit_code == 0, made.output
        assert f"closed commitment {cid}" in made.output
        assert commitment_status(conn, cid) == "dropped"

        listed = runner.invoke(cli.app, ["decisions"])
        assert "Sallie Mae application: not doing it" in listed.output
        assert "closed: Apply to Sallie Mae" in listed.output

        did = conn.execute("SELECT id FROM decision WHERE status='active'").fetchone()["id"]
        gone = runner.invoke(cli.app, ["decisions", "revisit", str(did)])
        assert gone.exit_code == 0, gone.output
        assert "no decisions recorded yet" in runner.invoke(cli.app, ["decisions"]).output
        # The commitment the decision closed stays closed.
        assert commitment_status(conn, cid) == "dropped"

    def test_refusals_exit_nonzero(self) -> None:
        from typer.testing import CliRunner

        from backglass import __main__ as cli

        runner = CliRunner()
        assert runner.invoke(cli.app, ["decisions", "record", " ", "choice"]).exit_code == 1
        assert (
            runner.invoke(
                cli.app, ["decisions", "record", "t", "c", "--closes", "99999"]
            ).exit_code
            == 1
        )
        assert runner.invoke(cli.app, ["decisions", "revisit", "4242"]).exit_code == 1
