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


def test_a_hand_typed_commitment_is_never_re_extracted(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """The 2026-08-27 duplication, as a test.

    `quick_add` stamps `extraction_version = 'manual'`, and 'manual' is not a prompt
    version, so it can never appear in `:compatible_versions`. Before the
    `source <> 'manual'` predicate, every prompt bump handed the owner's own sentence
    back to the extractor, which wrote a second commitment beside the one they had
    already typed — with a date read out of the prose rather than the date they had put
    in the field.

    Measured on the live ledger the day this was found: 37 manual source items, not one
    still carrying the 'manual' stamp, and 81 commitments hanging off 34 of them, two of
    which had grown seven each. 39 of the surplus were open.

    The assertion on `stamps` is the load-bearing half. Excluding by
    `extraction_version = 'manual'` would look like a fix and hold only until the next
    bump overwrote the stamp, which is exactly how this got here — so the test states
    that 'manual' is not, and cannot become, a version the queries accept.
    """
    from backglass.__main__ import EXTRACT_PROMPT
    from backglass.db import query
    from backglass.extract import prompts
    from backglass.ledger import USER_ID

    actions.quick_add(
        conn, settings, what="Book the Act 2 VR pod session", direction="i_owe",
        due_at="2026-09-03",
    )
    item = conn.execute("SELECT * FROM source_item WHERE source = 'manual'").fetchone()
    assert item["extraction_version"] == "manual"
    assert item["triage_verdict"] == "keep"

    stamps = ",".join(prompts.load(EXTRACT_PROMPT).stamps)
    assert "manual" not in stamps.split(","), (
        "'manual' is not a prompt version — the exclusion cannot rely on the stamp"
    )

    for name in ("pending_extraction", "pending_extraction_unbatched"):
        params: dict[str, object] = {"user_id": USER_ID, "compatible_versions": stamps}
        if name.endswith("unbatched"):
            params["cutoff"] = "2026-08-27T00:00:00Z"
        pending = conn.execute(query(name), params).fetchall()
        assert [r["id"] for r in pending] == [], (
            f"{name} handed a hand-typed commitment back to the extractor"
        )
