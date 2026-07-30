"""Quick-add: a hand-typed commitment with real provenance. Phase 6 S5."""

from __future__ import annotations

import sqlite3

import pytest

from backglass.config import Settings
from backglass.web import actions


def test_one_source_item_and_one_commitment_per_call(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    cid = actions.quick_add(
        conn, settings, what="Send Dana the deck", direction="i_owe",
        counterparty="Dana Wright <dana@fabriq.in>", due_at="2026-08-05",
    )
    items = conn.execute("SELECT * FROM source_item").fetchall()
    commitments = conn.execute("SELECT * FROM commitment").fetchall()
    assert len(items) == 1 and len(commitments) == 1
    row = commitments[0]
    assert row["id"] == cid
    assert row["source_item_id"] == items[0]["id"]
    assert row["confidence"] == 1.0
    assert items[0]["source"] == "manual"
    assert items[0]["triage_verdict"] == "keep"
    # The counterparty resolved to a real entity, so the People page sees them.
    assert row["counterparty_entity_id"] is not None
    name = conn.execute(
        "SELECT canonical_name FROM entity WHERE id = ?", (row["counterparty_entity_id"],)
    ).fetchone()["canonical_name"]
    assert name == "Dana Wright"


def test_manual_source_item_is_immutable_like_any_other(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    actions.quick_add(conn, settings, what="thing", direction="i_owe")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE source_item SET body_text = 'rewritten' WHERE source = 'manual'")


def test_direction_and_empty_text_are_validated(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    with pytest.raises(actions.ActionError):
        actions.quick_add(conn, settings, what="  ", direction="i_owe")
    with pytest.raises(actions.ActionError):
        actions.quick_add(conn, settings, what="x", direction="sideways")
    assert conn.execute("SELECT COUNT(*) AS n FROM source_item").fetchone()["n"] == 0


def test_merge_repoints_and_snapshots(conn: sqlite3.Connection, settings: Settings) -> None:
    from backglass.people import merge as merge_mod
    from backglass.people import profiles

    a = actions.person_create(conn, name="Ravi Menon", role="Partner")
    b = actions.person_create(conn, name="R. Menon")
    conn.execute(
        "UPDATE entity SET aliases_json = '[\"ravi@stellarcap.vc\"]' WHERE id = ?", (b,)
    )
    actions.quick_add(conn, settings, what="deck", direction="i_owe", counterparty="R. Menon")

    report = merge_mod.merge(conn, a, b)
    assert report["commitments_repointed"] == 1
    assert profiles.profile(conn, b) is None
    winner = profiles.profile(conn, a)
    assert winner is not None
    assert "ravi@stellarcap.vc" in winner["aliases"]
    assert "R. Menon" in winner["aliases"]
    snapshot = conn.execute("SELECT * FROM entity_merge").fetchone()
    assert snapshot["winner_id"] == a
    assert "R. Menon" in snapshot["loser_snapshot_json"]
    # Second merge of the same pair errors cleanly — the loser is gone.
    with pytest.raises(merge_mod.MergeError):
        merge_mod.merge(conn, a, b)
