"""The values nobody types, and the states nobody drives.

Every test here is a defect that a green suite of 1,441 tests did not see, because each
of those tests drives a value someone chose to write down. These were found by two
adversarial sweeps — every route against an empty ledger with hostile path and query
parameters, then every write against a ledger seeded with one of everything — and they
are kept as tests so the next such value is caught by CI rather than by a sweep.

Two of them lost data: a snooze large enough to overflow SQLite's date arithmetic
erased an open commitment's deadline, and quick-add wrote whatever was typed in the due
field straight into the column the board sorts by.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from backglass import __main__ as cli
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID, Ledger, LedgerError
from backglass.web import actions
from backglass.web.app import create_app

#: Larger than SQLite's INTEGER, which is where FastAPI's unbounded `int` used to hand
#: the driver an OverflowError instead of the route handing back a 404.
TOO_BIG = 10**30


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn  # migrated on disk; the app opens its own connections
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _source_item(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict, extraction_version)"
        " VALUES (?, 'manual', 'edge-1', ?, ?, 'a@example.com', 't', 'b', 'h', 'keep',"
        " 'manual')",
        (USER_ID, now_iso(), now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _open_commitment(conn: sqlite3.Connection, *, due_at: str = "2026-08-10") -> int:
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " source_item_id, created_at) VALUES (?, 'i_owe', 'seed', ?, 0.9, 'open', ?, ?)",
        (USER_ID, due_at, _source_item(conn), now_iso()),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestAMalformedDateIsAnAnswer:
    """`/brief/{on_date}` has always answered 422, because its parameter is typed
    `date`. The schedule pages parsed a string in the body and 500'd on everything
    `date.fromisoformat` cannot read — which includes a stale bookmark and a typo."""

    @pytest.mark.parametrize(
        "bad", ["2026-02-30", "not-a-date", "2026-8-5", "0000-01-01", "../../etc/passwd"]
    )
    def test_the_day_page_refuses_rather_than_raising(
        self, client: TestClient, bad: str
    ) -> None:
        assert client.get(f"/schedule?date={bad}").status_code == 422

    @pytest.mark.parametrize("bad", ["2026-02-30", "not-a-date", "9999-99-99"])
    def test_the_week_page_refuses_rather_than_raising(
        self, client: TestClient, bad: str
    ) -> None:
        assert client.get(f"/schedule/week?start={bad}").status_code == 422

    def test_a_representable_date_that_is_not_a_day_of_a_life(
        self, client: TestClient
    ) -> None:
        """`9999-12-31` parses, so it used to reach the handler — and then raised
        OverflowError inside `day_bounds`, which adds a day to find the day's end."""
        for path in ("/schedule?date=", "/schedule/week?start="):
            for absurd in ("9999-12-31", "0001-01-01"):
                assert client.get(path + absurd).status_code == 422, path + absurd

    def test_the_edges_of_the_window_still_render(self, client: TestClient) -> None:
        for path in ("/schedule?date=", "/schedule/week?start="):
            for edge in ("1900-01-01", "2200-01-01"):
                assert client.get(path + edge).status_code == 200, path + edge

    @pytest.mark.parametrize("command", ["plan", "brief", "shutdown"])
    def test_the_cli_says_which_option_is_wrong(
        self, command: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A traceback tells the owner they broke the program. One of the CLI's date
        options (`log --on`) said so in a sentence; the rest raised."""
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        result = CliRunner().invoke(cli.app, [command, "--date", "2026-02-30"])
        assert result.exit_code == 1
        assert "not a date" in result.output
        assert "Traceback" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


class TestASnoozeCannotEraseTheDeadline:
    """SQLite's `date(x, '+N days')` returns NULL rather than raising when N overflows
    its own arithmetic, and a NULL `due_at` is not an error anywhere in this codebase —
    it means "no deadline". So the write reported "snoozed" and the commitment lost the
    date the board sorts it by, with nothing to say so."""

    def test_a_snooze_past_the_bound_is_refused(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = _open_commitment(conn)
        response = client.post(f"/commitments/{cid}/snooze/{10**15}")
        assert response.status_code == 422
        assert "not a snooze" in response.json()["detail"]
        after = conn.execute("SELECT due_at FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert after["due_at"] == "2026-08-10", "the refusal wrote nothing"

    def test_an_ordinary_snooze_still_works(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = _open_commitment(conn)
        assert client.post(f"/commitments/{cid}/snooze/7").status_code == 200
        after = conn.execute("SELECT due_at FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert after["due_at"] == "2026-08-17"


class TestQuickAddWritesADate:
    """The extraction door resolves every date through `extract/dates.resolve_due`. The
    owner's own door put the form field into the column as typed."""

    def test_a_phrase_is_resolved_the_way_extraction_resolves_one(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        typed = {"what": "call the registrar", "due_at": "friday"}
        assert client.post("/commitments/quick-add", data=typed).status_code == 200
        stored = conn.execute(
            "SELECT due_at FROM commitment ORDER BY id DESC LIMIT 1"
        ).fetchone()["due_at"]
        assert stored is not None
        assert date.fromisoformat(str(stored)[:10]) >= date.today()

    @pytest.mark.parametrize("bad", ["not-a-date", "2026-02-30", "9999-99-99", "x" * 300])
    def test_an_unreadable_date_is_refused_rather_than_stored(
        self, client: TestClient, conn: sqlite3.Connection, bad: str
    ) -> None:
        response = client.post(
            "/commitments/quick-add", data={"what": "a thing", "due_at": bad}
        )
        assert response.status_code == 422
        assert "could not read" in response.json()["detail"]
        assert conn.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM source_item").fetchone()["n"] == 0, (
            "a refused quick-add must not leave the owner's words in the ledger with no "
            "commitment on them"
        )

    def test_no_due_date_is_still_legal(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        assert (
            client.post("/commitments/quick-add", data={"what": "someday"}).status_code == 200
        )
        assert (
            conn.execute("SELECT due_at FROM commitment").fetchone()["due_at"] is None
        )

    def test_a_commitment_is_a_line_not_a_document(self, client: TestClient) -> None:
        response = client.post("/commitments/quick-add", data={"what": "x" * 20_000})
        assert response.status_code == 422
        assert "not a document" in response.json()["detail"]

    def test_the_ledger_itself_refuses_a_date_shaped_string(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The guard is at the INSERT, not only at the door that was wrong: the next
        door has not been written yet."""
        ledger = Ledger(conn, settings)
        with pytest.raises(LedgerError, match="not a due date"):
            ledger.insert_commitment(
                direction="i_owe",
                entity_id=None,
                what="w",
                due_at="tomorrow",
                estimated_minutes=None,
                estimate_source=None,
                confidence=1.0,
                source_item_id=_source_item(conn),
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"] == 0

    @pytest.mark.parametrize("good", [None, "2026-08-10", "2026-08-10T17:00:00-07:00"])
    def test_the_shapes_extraction_produces_are_accepted(
        self, conn: sqlite3.Connection, settings: Settings, good: str | None
    ) -> None:
        Ledger(conn, settings).insert_commitment(
            direction="i_owe",
            entity_id=None,
            what="w",
            due_at=good,
            estimated_minutes=None,
            estimate_source=None,
            confidence=1.0,
            source_item_id=_source_item(conn),
        )


class TestAnIdTooLargeForTheDatabase:
    """FastAPI's `int` is Python's, which is unbounded; SQLite's is 64-bit. Twenty
    routes reached the driver and died there with OverflowError."""

    @pytest.mark.parametrize(
        "path",
        [
            "/people/{id}",
            "/source/{id}",
            "/roadmaps/{id}",
            "/b/{id}.gif",
        ],
    )
    def test_a_read_answers_instead_of_raising(self, client: TestClient, path: str) -> None:
        assert client.get(path.format(id=TOO_BIG)).status_code == 422

    @pytest.mark.parametrize(
        "path",
        [
            "/commitments/{id}/resolve",
            "/commitments/{id}/drop",
            "/review/{id}/accept",
            "/review/plan/{id}/reject",
            "/checklist/{id}/tick",
            "/checklist/{id}/untick",
            "/people/{id}/edit",
            "/roadmaps/{id}/drop",
            "/chats/{id}/monitor",
            "/memory/{id}/forget",
        ],
    )
    def test_a_write_answers_instead_of_raising(self, client: TestClient, path: str) -> None:
        assert client.post(path.format(id=TOO_BIG)).status_code == 422

    def test_a_rowid_is_at_least_one(self, client: TestClient) -> None:
        """SQLite hands rowids out from 1 upward, so 0 and the negatives name nothing."""
        assert client.get("/people/0").status_code == 422
        assert client.post("/commitments/-1/resolve").status_code == 422


class TestAStaleTabIsToldWhy:
    """Every action in `web/actions.py` checks its row before writing — except the two
    that failed in opposite directions: `tick` let a FOREIGN KEY failure escape as a
    500, and `untick` deleted nothing and reported "unticked"."""

    def test_ticking_a_vanished_item_is_refused(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute("INSERT INTO checklist_item (user_id, title) VALUES (?, 'x')", (USER_ID,))
        item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
        assert client.post(f"/checklist/{item_id}/tick").status_code == 200

        conn.execute("DELETE FROM checklist_item WHERE id = ?", (item_id,))
        conn.commit()
        for verb in ("tick", "untick"):
            response = client.post(f"/checklist/{item_id}/{verb}")
            assert response.status_code == 422, verb
            assert "no checklist item" in response.json()["detail"], verb


class TestNumbersThatAreNotNumbers:
    """The convention of `goals/activities.MAX_HOURS_PER_ENTRY`: a bound far above any
    real entry, so it can only ever catch a mistake."""

    def test_an_estimate_longer_than_a_working_life(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = _open_commitment(conn)
        response = client.post(f"/commitments/{cid}/estimate/{10**15}")
        assert response.status_code == 422
        assert "not an estimate" in response.json()["detail"]
        assert (
            conn.execute(
                "SELECT estimated_minutes FROM commitment WHERE id = ?", (cid,)
            ).fetchone()["estimated_minutes"]
            is None
        )

    def test_a_cadence_of_a_quadrillion_a_week(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, definition_of_done, status,"
            " created_at) VALUES (?, 'g', 'annual', 'd', 'active', ?)",
            (USER_ID, now_iso()),
        )
        goal_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at, user_id)"
            " VALUES (?, 'cadence', 't', 3, ?, ?)",
            (goal_id, now_iso(), USER_ID),
        )
        target_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()

        response = client.post(f"/targets/{target_id}/weekly/{10**15}")
        assert response.status_code == 422
        assert "not a cadence" in response.json()["detail"]
        assert (
            conn.execute(
                "SELECT weekly_count FROM target WHERE id = ?", (target_id,)
            ).fetchone()["weekly_count"]
            == 3
        )

    def test_the_ordinary_values_are_untouched(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """A bound that catches a real entry is a worse bug than the one it fixed."""
        cid = _open_commitment(conn)
        assert client.post(f"/commitments/{cid}/estimate/45").status_code == 200
        assert actions.MAX_ESTIMATE_MINUTES > 60 * 24
        assert actions.MAX_SNOOZE_DAYS > 365
