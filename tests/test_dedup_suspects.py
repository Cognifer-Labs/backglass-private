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
    source_item_id: int | None = None,
    counterparty: str | None = None,
) -> int:
    """`source_item_id` reuses one message for several promises — the fan-out shape,
    where one mail names three people. `counterparty` is who this half is owed to."""
    if source_item_id is None:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
            " author, title, body_text, raw_json, content_hash, triage_verdict)"
            " VALUES (?, 'gmail:personal', ?, ?, '2026-07-14T09:15:00-07:00',"
            " 'Dana <dana@example.gov>', 'Plan', 'b', '{}', ?, 'keep')",
            (USER_ID, f"m-{what}-{direction}-{status}", now_iso(),
             f"h-{what}-{direction}-{status}"),
        )
        source_item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    source_id = source_item_id
    entity_id: int | None = None
    if counterparty:
        # (user_id, kind, canonical_name) is unique, and two promises to one person are
        # the ordinary case, so this reuses rather than inserts a second Dana.
        existing = conn.execute(
            "SELECT id FROM entity WHERE user_id = ? AND kind = 'person'"
            " AND canonical_name = ?",
            (USER_ID, counterparty),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO entity (user_id, kind, canonical_name, aliases_json, tags_json)"
                " VALUES (?, 'person', ?, '[]', '[]')",
                (USER_ID, counterparty),
            )
            entity_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        else:
            entity_id = int(existing["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " source_item_id, counterparty_entity_id, created_at)"
        " VALUES (?, ?, ?, ?, 0.9, ?, ?, ?, ?)",
        (USER_ID, direction, what, due_at, status, source_id, entity_id, now_iso()),
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


class TestThePairCarriesWhatWouldAnswerIt:
    """A question about two identical sentences is not a question.

    The owner's queue holds 75 open pairs, 19 of them one message that promised
    something to each of several people. Source item 8763 asked for an intro email to
    Suriyampola, Hossain and Pedram; extraction wrote all three as "Send instructor intro
    email from ASU address". Same or Different, over two rows reading the same words with
    no other fact on them, is a coin toss — so the pairs stayed open and the queue stopped
    being worth opening.
    """

    def _three_instructors(self, conn: sqlite3.Connection) -> None:
        first = a_commitment(conn, "Send instructor intro email", counterparty="Suriyampola")
        source = int(
            conn.execute(
                "SELECT source_item_id AS s FROM commitment WHERE id = ?", (first,)
            ).fetchone()["s"]
        )
        a_commitment(
            conn, "Send instructor intro email", source_item_id=source, counterparty="Hossain"
        )

    def test_each_side_names_its_counterparty(self, conn: sqlite3.Connection) -> None:
        self._three_instructors(conn)
        pair = dedup.suspects(conn)[0]
        assert {pair["a_who"], pair["b_who"]} == {"Suriyampola", "Hossain"}

    def test_one_message_to_two_people_is_labelled_and_still_asked(
        self, conn: sqlite3.Connection
    ) -> None:
        """Labelled, not hidden and not merged. The same shape covers one task read
        twice with the sender resolved differently each time — "Complete the Math
        Placement Test" in the owner's store — so the fact decides the question and the
        owner still answers it."""
        self._three_instructors(conn)
        suspects = dedup.suspects(conn)
        assert len(suspects) == 1
        assert suspects[0]["one_message"] is True

    def test_two_messages_about_one_promise_are_not_labelled_a_fan_out(
        self, conn: sqlite3.Connection
    ) -> None:
        a_commitment(conn, "send Dana the housing addendum", counterparty="Dana")
        a_commitment(conn, "housing addendum to Dana", counterparty="Dana")
        assert dedup.suspects(conn)[0]["one_message"] is False

    def test_a_pair_with_no_counterparty_resolved_says_nothing_rather_than_None(
        self, conn: sqlite3.Connection
    ) -> None:
        """A row whose sender never resolved is common, and "— None" on the card is
        worse than no attribution at all."""
        a_commitment(conn, "send Dana the housing addendum")
        a_commitment(conn, "housing addendum to Dana")
        pair = dedup.suspects(conn)[0]
        assert pair["a_who"] == "" and pair["b_who"] == ""


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
