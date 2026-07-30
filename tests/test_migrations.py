"""docs/10 §Storage: "Apply in order, record the version, refuse to start if the file set
and the recorded version disagree."
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backglass.db import MigrationError, connect, migrate


def test_init_creates_the_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "a.db")
    applied = migrate(conn)
    assert applied == [1, 2, 3, 4, 5]

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
    assert migrate(conn) == [1, 2, 3, 4, 5]
    assert migrate(conn) == []
    assert migrate(conn) == []
    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_version")]
    assert versions == [1, 2, 3, 4, 5]
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
