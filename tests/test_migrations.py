"""docs/10 §Storage: "Apply in order, record the version, refuse to start if the file set
and the recorded version disagree."
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backglass.db import QUERIES_DIR, MigrationError, connect, migrate

MIGRATIONS_DIR = QUERIES_DIR.parent / "migrations"


def _versions_on_disk() -> list[int]:
    """Derived, never a literal: a hardcoded list makes every new migration fail
    tests that are not about it, which teaches the next author to edit the
    assertion rather than read it."""
    return sorted(
        int(path.name[:4])
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
    )


def test_init_creates_the_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "a.db")
    applied = migrate(conn)
    # Derived from the files rather than a literal: a hardcoded list makes every new
    # migration fail a test that is not about the new migration, which trains whoever
    # adds one to edit the assertion instead of reading it.
    expected = _versions_on_disk()
    assert applied == expected
    assert expected[0] == 1 and expected == list(range(1, len(expected) + 1)), (
        "migrations must be numbered contiguously from 0001"
    )

    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    # The thirteen tables docs/10 promises, plus schema_version.
    assert {
        "credential",
        "source_item",
        "entity",
        "goal",
        "target",
        "commitment",
        "checkpoint",
        "checklist_item",
        "checklist_tick",
        "day_plan",
        "plan_block",
        "shutdown_note",
        "brief",
        "run",
        "roadmap",
        "roadmap_step",
        "roadmap_cadence",
        "entity_merge",
        "schema_version",
    } <= tables
    conn.close()


def test_init_is_idempotent(tmp_path: Path) -> None:
    """`backglass init` is safe to run repeatedly — the first assertion of rule 3."""
    path = tmp_path / "b.db"
    conn = connect(path)
    assert migrate(conn) == _versions_on_disk()
    assert migrate(conn) == []
    assert migrate(conn) == []
    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_version")]
    assert versions == _versions_on_disk()
    conn.close()


def test_pragmas_are_set_on_every_connection(tmp_path: Path) -> None:
    conn = connect(tmp_path / "c.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()["journal_mode"] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"] == 1
    conn.close()


def test_editing_an_applied_migration_refuses_to_start(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The failure mode that actually bites: a migration edited after it shipped leaves
    every other copy of the database on a schema this one has never seen."""
    import backglass.db as db

    staging = tmp_path / "migrations"
    staging.mkdir()
    (staging / "0001_initial.sql").write_text(
        "CREATE TABLE schema_version (\n"
        " version INTEGER PRIMARY KEY, filename TEXT NOT NULL, checksum TEXT NOT NULL,"
        " applied_at TEXT NOT NULL);\nCREATE TABLE thing (id INTEGER PRIMARY KEY);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staging)

    conn = connect(tmp_path / "d.db")
    assert migrate(conn) == [1]

    (staging / "0001_initial.sql").write_text(
        (staging / "0001_initial.sql").read_text() + "\n-- a later edit\n"
    )
    with pytest.raises(MigrationError, match="immutable"):
        migrate(conn)
    conn.close()


def test_a_missing_applied_migration_refuses_to_start(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import backglass.db as db

    staging = tmp_path / "migrations"
    staging.mkdir()
    (staging / "0001_initial.sql").write_text(
        "CREATE TABLE schema_version (\n"
        " version INTEGER PRIMARY KEY, filename TEXT NOT NULL, checksum TEXT NOT NULL,"
        " applied_at TEXT NOT NULL);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staging)

    conn = connect(tmp_path / "e.db")
    migrate(conn)
    (staging / "0001_initial.sql").unlink()
    with pytest.raises(MigrationError, match="not on disk"):
        migrate(conn)
    conn.close()


def test_source_item_is_immutable_except_the_three_extraction_columns(tmp_path: Path) -> None:
    """docs/03: raw items are written once and never modified.

    Enforced by a trigger rather than a convention, for the same reason boundary.py lives
    in the connector package — bypassing it has to be a visible edit.
    """
    conn = connect(tmp_path / "f.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
        " body_text, content_hash) "
        "VALUES (1, 'gmail:personal', 'x', 'now', 'then', 'body', 'h')"
    )

    # The documented exceptions are allowed.
    conn.execute(
        "UPDATE source_item SET triage_verdict = 'keep', triage_reason = 'r' WHERE id = 1"
    )
    conn.execute(
        "UPDATE source_item SET extraction_version = 'extract-commitments@1' WHERE id = 1"
    )

    for column, value in (
        ("body_text", "rewritten"),
        ("occurred_at", "2020-01-01"),
        ("content_hash", "other"),
        ("external_id", "y"),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"UPDATE source_item SET {column} = ? WHERE id = 1", (value,))
    conn.close()


# ── the shipped migrations are byte-frozen ────────────────────────────────

#: sha256 of every migration file, as applied to the owner's database. `migrate()`
#: refuses to start when a recorded checksum and the file on disk disagree, so an
#: edit to an already-applied migration bricks every existing database — including
#: one made for a reason that looks harmless. It happened on 2026-08-02: the
#: public-release scrub rewrote a *comment* in 0007 to drop a private-doc citation,
#: and every `backglass` command against the owner's db died on MigrationError until
#: the recorded checksum was resealed. The runtime guard above only fires on a
#: machine that already has the old bytes; this one fires in CI, before the edit
#: ships. Add a line here when you add a migration; never change one.
FROZEN_CHECKSUMS = {
    "0001_initial.sql": ("0bbd99e163d1cd119b7cdf97856f8b5934baadea4769e21722022336a8a4c879"),
    "0002_source_item_immutable.sql": (
        "e96c9786f0a3d099e0d71dbadd1db5ba19e122b57bbbb0d05817d33b71f2c014"
    ),
    "0003_career_layer.sql": (
        "8aa612cc1bc42d4a8484a2a9fe08fb34ba7452d7d267f78c6de800c7ac94c841"
    ),
    "0004_source_toggle.sql": (
        "a4afec673f6ca51ed748fed271c1ecb44aa39c504d9a3f2768de674d3934887d"
    ),
    "0005_source_item_delete_guard.sql": (
        "8c7495872d47097945df185489bc430f565961d71ff76d1a4898719eaa72b0b9"
    ),
    "0006_total_targets.sql": (
        "6788815070cdac5593b5657abdf57ff734e57c181ebd6cc9970eb28d167d52be"
    ),
    "0007_activities.sql": ("c8f4fa7383caf410a08b5ba4dd01f240e9ed4d11eb732951baa3f01675adac67"),
    "0008_facts.sql": ("b52dc047f4c0699dc6af2d4404cde42133b9f2d9260f54b5c4ce29f1dc4d4aed"),
    "0009_learned_noise.sql": (
        "4a0efb2386673f7feca8046ac261bc4f7fccb82e07ba5cf7691725ad9146f0bc"
    ),
    "0010_template_hash.sql": (
        "8252bc7825d6e371c68e2ea8c724b9dd209b1dbbce9aebba39ba3040f12f79bc"
    ),
    "0011_model_batch.sql": (
        "9c42a187902517ca3117740f19d7617d058ee5885dc41478a76196a4f6b6277e"
    ),
    "0012_user_id_everywhere.sql": (
        "3787276d05174c3661d43d2a6b39fb4bf767a94dbf8cb0ce973b329f4d9e4b05"
    ),
    "0013_commitment_evidence.sql": (
        "1a46f91cb48df63fa9bba74d1f934eb1076023684868428c8798bc6a1179d242"
    ),
    "0014_engagements.sql": (
        "f0de85ad6651de7e4b0f55bd725817e06197b2a18cf15883dc14cd64a3107499"
    ),
    "0015_monitored_chats.sql": (
        "08e897e5f38ec4a2eb46b0c6ea2ae22f83e24ae557fa7181237f394148765348"
    ),
}


def _migration_dir() -> Path:
    from backglass.db import MIGRATIONS_DIR

    return MIGRATIONS_DIR


def test_no_shipped_migration_has_been_edited() -> None:
    import hashlib

    for path in sorted(_migration_dir().glob("*.sql")):
        expected = FROZEN_CHECKSUMS.get(path.name)
        if expected is None:
            continue  # a new migration; the next test makes sure it gets a line here
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, (
            f"{path.name} was edited. Migrations are immutable once applied — every "
            f"database that already ran it will refuse to start. Add a new migration "
            f"instead; if the edit is genuinely unshipped, update FROZEN_CHECKSUMS."
        )


def test_every_migration_is_frozen() -> None:
    on_disk = {path.name for path in _migration_dir().glob("*.sql")}
    assert on_disk == set(FROZEN_CHECKSUMS), (
        "a migration is missing from FROZEN_CHECKSUMS (or listed there but deleted) — "
        "an unfrozen migration can be edited silently, which is the exact failure this "
        "pair of tests exists to stop"
    )
