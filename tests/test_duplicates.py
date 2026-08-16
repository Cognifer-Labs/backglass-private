"""Duplicate suspects as clusters. See backglass/duplicates.py for why.

`dedup.suspects` already found these — 264 open pairs on the owner's ledger on
2026-08-15, including every duplicate that was costing the planner real time. What was
missing is a unit anybody can answer: three restatements of one promise are three pairs
and one decision.
"""

from __future__ import annotations

import sqlite3

from backglass import duplicates
from backglass.ledger import USER_ID


def _commitment(
    conn: sqlite3.Connection,
    what: str,
    *,
    source_item_id: int | None = None,
    counterparty: int | None = None,
) -> int:
    if source_item_id is None:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, title, body_text, content_hash, triage_verdict,"
            " extraction_version) VALUES (1, 'manual', ?, '2026-08-10T00:00:00Z',"
            " '2026-08-10T00:00:00Z', ?, ?, ?, 'keep', 'manual')",
            (f"ext-{what}", what, what, f"hash-{what}"),
        )
        source_item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " estimated_minutes, estimate_source, source_item_id, counterparty_entity_id,"
        " created_at) VALUES (1, 'i_owe', ?, 0.9, 'open', 30, 'manual', ?, ?,"
        " '2026-08-10T00:00:00Z')",
        (what, source_item_id, counterparty),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _entity(conn: sqlite3.Connection, name: str) -> int:
    conn.execute(
        "INSERT INTO entity (user_id, kind, canonical_name) VALUES (1, 'person', ?)",
        (name,),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _item(conn: sqlite3.Connection, tag: str) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict, extraction_version)"
        " VALUES (1, 'manual', ?, '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z',"
        " ?, ?, ?, 'keep', 'manual')",
        (tag, tag, tag, tag),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestClustering:
    def test_three_restatements_are_one_decision(self, conn: sqlite3.Connection) -> None:
        """The point of the module. Pairwise, this is three questions asked separately,
        and nothing stops the owner answering them inconsistently."""
        for text in (
            "Submit Hospice of the Valley volunteer application at volunteers.hov.org",
            "submit volunteer application on HOV website",
            "submit volunteer application online",
        ):
            _commitment(conn, text)
        found = duplicates.clusters(conn)
        assert len(found) == 1
        assert len(found[0].members) == 3

    def test_the_survivor_is_the_row_that_says_the_most(
        self, conn: sqlite3.Connection
    ) -> None:
        # The live pair, which scored 0.82 on the owner's ledger.
        rich = _commitment(conn, "Submit Hospice of the Valley volunteer application")
        _commitment(conn, "submit volunteer application online")
        cluster = duplicates.clusters(conn)[0]
        assert int(cluster.survivor["id"]) == rich
        assert [int(m["id"]) for m in cluster.losers] == [
            i for i in cluster.ids if i != rich
        ]

    def test_the_note_names_the_survivor(self, conn: sqlite3.Connection) -> None:
        """A dropped row has to say what it was dropped in favour of, or the tombstone
        is a claim with nothing behind it."""
        _commitment(conn, "accept Academic Excellence Scholarship award")
        _commitment(conn, "accept the Academic Excellence Scholarship award now")
        cluster = duplicates.clusters(conn)[0]
        assert cluster.note() == f"duplicate of commitment {cluster.survivor['id']}"

    def test_unrelated_commitments_do_not_cluster(self, conn: sqlite3.Connection) -> None:
        _commitment(conn, "pay the housing deposit")
        _commitment(conn, "read chapter four of the biochemistry text")
        assert duplicates.clusters(conn) == []

    def test_a_pair_the_owner_kept_apart_never_returns(
        self, conn: sqlite3.Connection
    ) -> None:
        """`commitment_distinct` is the memory; this must honour it or the queue asks
        the same settled question every morning."""
        from backglass.web import actions

        a = _commitment(conn, "submit volunteer application on HOV website")
        b = _commitment(conn, "submit volunteer application online")
        actions.different(conn, a, b)
        assert duplicates.clusters(conn) == []


class TestFanOut:
    def test_one_message_to_three_people_is_labelled_not_collapsed(
        self, conn: sqlite3.Connection
    ) -> None:
        """The trap. Three intro emails to three instructors have identical text and are
        three real promises; collapsing them destroys two."""
        item = _item(conn, "intro-emails")
        for name in ("Suriyampola", "Hossain", "Pedram"):
            _commitment(
                conn,
                "Send instructor intro email from ASU address",
                source_item_id=item,
                counterparty=_entity(conn, name),
            )
        cluster = duplicates.clusters(conn)[0]
        assert cluster.identical
        assert cluster.kind == duplicates.FAN_OUT

    def test_identical_text_from_two_messages_is_not_a_fan_out(
        self, conn: sqlite3.Connection
    ) -> None:
        """Two separate emails restating one task is the ordinary duplicate, and the
        label must not protect it."""
        _commitment(
            conn, "Upload ASU ID photo and verify identity",
            counterparty=_entity(conn, "Sun Devil Card Services"),
        )
        _commitment(
            conn, "upload ASU ID photo and verify identity",
            counterparty=_entity(conn, "Arizona State University"),
        )
        cluster = duplicates.clusters(conn)[0]
        assert cluster.identical
        assert cluster.kind == duplicates.RESTATEMENT


class TestTheCommandWritesNothingByDefault:
    def test_dry_run_leaves_every_row_open(self, conn: sqlite3.Connection) -> None:
        from typer.testing import CliRunner

        from backglass.__main__ import app

        _commitment(conn, "Upload ASU ID photo and verify identity")
        _commitment(conn, "upload ASU ID photo and verify identity")
        conn.commit()
        result = CliRunner().invoke(app, ["duplicates"])
        assert result.exit_code == 0, result.output
        assert "nothing written" in result.output
        open_now = conn.execute(
            "SELECT COUNT(*) AS n FROM commitment WHERE user_id = ? AND status = 'open'",
            (USER_ID,),
        ).fetchone()["n"]
        assert open_now == 2
