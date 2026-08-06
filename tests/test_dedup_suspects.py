"""Look-alike open commitments: found once, asked once, answered forever."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backglass import dedup
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.web import actions
from backglass.web.app import create_app
from tests.conftest import panel_slice

TODAY = date.today()


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def a_commitment(
    conn: sqlite3.Connection,
    what: str,
    *,
    direction: str = "i_owe",
    due_at: str | None = None,
    status: str = "open",
    quote: str | None = None,
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'gmail:personal', ?, ?, '2026-07-14T09:15:00-07:00',"
        " 'Dana <dana@example.gov>', 'Plan', 'b', '{}', ?, 'keep')",
        (USER_ID, f"m-{what}-{direction}-{status}", now_iso(),
         f"h-{what}-{direction}-{status}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " source_item_id, created_at) VALUES (?, ?, ?, ?, 0.9, ?, ?, ?)",
        (USER_ID, direction, what, due_at, status, source_id, now_iso()),
    )
    cid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment_evidence (user_id, commitment_id, source_item_id,"
        " quote, kind, seen_at) VALUES (?, ?, ?, ?, 'original', ?)",
        (USER_ID, cid, source_id, quote, now_iso()),
    )
    return cid


class TestSuspects:
    def test_a_lookalike_pair_is_found_and_ranked(
        self, conn: sqlite3.Connection
    ) -> None:
        a_commitment(conn, "send Dana the housing addendum")
        a_commitment(conn, "housing addendum to Dana")
        a_commitment(conn, "renew the parking permit")  # noise, no pair
        pairs = dedup.suspects(conn)
        assert len(pairs) == 1
        assert pairs[0]["a_what"] == "send Dana the housing addendum"
        assert pairs[0]["score"] >= dedup.SUSPECT_FLOOR

    def test_direction_separates_the_two_halves_of_one_exchange(
        self, conn: sqlite3.Connection
    ) -> None:
        """"Send the deck" owed both ways is a handoff, not a duplicate."""
        a_commitment(conn, "send the deck", direction="i_owe")
        a_commitment(conn, "send the deck", direction="owed_to_me")
        assert dedup.suspects(conn) == []

    def test_an_answered_pair_is_never_asked_again(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a = a_commitment(conn, "send Dana the housing addendum")
        b = a_commitment(conn, "housing addendum to Dana")
        actions.different(conn, b, a)  # reversed order on purpose — pair is normalized
        conn.commit()
        assert dedup.suspects(conn) == []
        with pytest.raises(actions.ActionError):
            actions.different(conn, a, b)  # answering twice is acting on nothing


class TestSameThing:
    def test_the_older_row_wins_and_keeps_everything(
        self, conn: sqlite3.Connection
    ) -> None:
        a = a_commitment(conn, "send Dana the housing addendum",
                         due_at="2026-08-20", quote="can you send the addendum?")
        b = a_commitment(conn, "housing addendum to Dana",
                         due_at="2026-08-10", quote="the addendum by the 10th please")
        actions.same_thing(conn, a, b)
        conn.commit()
        loser = conn.execute("SELECT * FROM commitment WHERE id = ?", (b,)).fetchone()
        assert loser["status"] == "superseded"
        assert loser["superseded_by"] == a
        winner = conn.execute("SELECT * FROM commitment WHERE id = ?", (a,)).fetchone()
        assert winner["status"] == "open"
        assert winner["due_at"] == "2026-08-10"  # the safer deadline survives
        quotes = {
            str(r["quote"])
            for r in conn.execute(
                "SELECT quote FROM commitment_evidence WHERE commitment_id = ?", (a,)
            )
        }
        assert "the addendum by the 10th please" in quotes  # citations moved across

    def test_refusals_leave_both_rows_alone(self, conn: sqlite3.Connection) -> None:
        a = a_commitment(conn, "send the deck")
        done = a_commitment(conn, "deck to Dana", status="done")
        with pytest.raises(actions.ActionError):
            actions.same_thing(conn, a, done)
        with pytest.raises(actions.ActionError):
            actions.same_thing(conn, a, a)
        with pytest.raises(actions.ActionError):
            actions.same_thing(conn, a, 99999)
        row = conn.execute("SELECT status FROM commitment WHERE id = ?", (a,)).fetchone()
        assert row["status"] == "open"


class TestTheBoardAsksTheQuestion:
    def test_the_fold_renders_and_both_answers_work(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        a = a_commitment(conn, "send Dana the housing addendum")
        b = a_commitment(conn, "housing addendum to Dana")
        conn.commit()
        board = panel_slice(client.get("/").text, "panel-board")
        assert "Looks the same (1)" in board

        merged = client.post(f"/commitments/{a}/same/{b}")
        assert merged.status_code == 200
        assert "Looks the same (" not in merged.text
        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (b,)
        ).fetchone()["status"] == "superseded"

    def test_different_silences_the_pair_through_the_real_door(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        a = a_commitment(conn, "send Dana the housing addendum")
        b = a_commitment(conn, "housing addendum to Dana")
        conn.commit()
        kept = client.post(f"/commitments/{a}/distinct/{b}")
        assert kept.status_code == 200
        assert "Looks the same (" not in kept.text
        # Asked-and-answered stays answered on the next full page load too.
        assert "Looks the same (" not in client.get("/").text
        assert client.post(f"/commitments/{b}/distinct/{a}").status_code == 422
