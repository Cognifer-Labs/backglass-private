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


def _engagement(
    conn: sqlite3.Connection,
    what: str,
    starts_at: str | None,
    *,
    status: str = "confirmed",
    ends_at: str | None = None,
    location: str | None = None,
) -> int:
    item = _item(conn, f"eng-{what}-{starts_at}-{status}")
    conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at, location,"
        " status, confidence, source_item_id, created_at)"
        " VALUES (1, 'social', ?, ?, ?, ?, ?, 0.9, ?, '2026-08-01T00:00:00Z')",
        (what, starts_at, ends_at, location, status, item),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestPlanClusters:
    """Engagements had no review surface at all, and the duplication is worse there: six
    rows describe one kickoff dinner, twenty-three describe one move-in."""

    def test_one_dinner_written_three_ways_is_one_cluster(
        self, conn: sqlite3.Connection
    ) -> None:
        for what in (
            "McKenna Program Welcome Dinner",
            "McKenna Program welcome dinner",
            "McKenna Program Welcome Dinner tonight",
        ):
            _engagement(conn, what, "2026-08-09T18:00:00")
        clusters, _ = duplicates.plan_clusters(conn)
        assert len(clusters) == 1
        assert len(clusters[0].members) == 3
        assert clusters[0].day == "2026-08-09"

    def test_the_same_words_on_another_day_are_a_series_not_a_duplicate(
        self, conn: sqlite3.Connection
    ) -> None:
        """The engagement equivalent of the fan-out trap. A weekly standing arrangement
        is the same sentence every week; fusing them would swallow every occurrence."""
        _engagement(conn, "pickleball at Pecos", "2026-08-03T18:30:00")
        _engagement(conn, "pickleball at Pecos", "2026-08-10T18:30:00")
        _engagement(conn, "pickleball at Pecos", "2026-08-17T18:30:00")
        clusters, _ = duplicates.plan_clusters(conn)
        assert clusters == []

    def test_undated_rows_are_counted_not_silently_dropped(
        self, conn: sqlite3.Connection
    ) -> None:
        """Most of the move-in rows have no date, so there is no day to anchor them in —
        and a report that omits them reads as though it covered everything."""
        _engagement(conn, "ASU dorm move-in", None)
        _engagement(conn, "ASU move-in", None)
        clusters, undated = duplicates.plan_clusters(conn)
        assert clusters == []
        assert undated == 2

    def test_a_confirmed_plan_outranks_a_proposal(self, conn: sqlite3.Connection) -> None:
        """A plan the owner agreed to is the one worth keeping."""
        _engagement(conn, "McKenna kickoff dinner tonight", "2026-08-09T18:00:00",
                    status="proposed")
        keep = _engagement(conn, "McKenna kickoff dinner", "2026-08-09T18:00:00",
                           status="confirmed", location="Armstrong Hall")
        [cluster] = duplicates.plan_clusters(conn)[0]
        assert int(cluster.survivor["id"]) == keep

    def test_the_day_is_a_string_prefix_and_never_a_normalized_instant(
        self, conn: sqlite3.Connection
    ) -> None:
        """`engagement.starts_at` holds bare dates, naive local datetimes and
        offset-bearing strings side by side. SQLite's `datetime()` would normalise the
        offset-bearing rows to UTC and march a 19:00 Phoenix dinner into the next day —
        the failure recorded for 2026-08-01."""
        _engagement(conn, "college dinner", "2026-08-09T19:00:00-07:00")
        _engagement(conn, "college dinner tonight", "2026-08-09T19:00:00-07:00")
        [cluster] = duplicates.plan_clusters(conn)[0]
        # 2026-08-09T19:00-07:00 is 2026-08-10T02:00 UTC. The local day is what matters.
        assert cluster.day == "2026-08-09"

    def test_a_declined_plan_is_not_a_duplicate_of_a_live_one(
        self, conn: sqlite3.Connection
    ) -> None:
        _engagement(conn, "early move-in", "2026-08-09T08:00:00")
        _engagement(conn, "early move-in", "2026-08-09T08:00:00", status="declined")
        assert duplicates.plan_clusters(conn)[0] == []
