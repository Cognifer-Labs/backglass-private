"""`specs/schema.sql` must describe the database the migrations actually build.

CLAUDE.md's reading order sends a newcomer to `specs/schema.sql` as step 3 — before any
code. On 2026-08-01 that file described 14 tables against 25 live ones: everything from
migrations 0003 onward (`fact`, `activity`, the roadmap tables, `entity_merge`,
`learned_noise`, the batch tables) was missing, and it still claimed `user_id` was on
every table when eight tables had never had it. A reference that is only sometimes true
is worse than no reference, because it is read instead of the schema.

Hand-syncing is what failed, so it is not the fix. The file is generated from a freshly
migrated database and this test fails the moment the two disagree — the drift is caught
by CI-equivalent rather than by the next person to be misled.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backglass.db import connect, migrate

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "specs" / "schema.sql"

HEADER = """\
-- Backglass schema. SQLite.
--
-- GENERATED from backglass/db/migrations/ — do not hand-edit. Change the schema by
-- adding a migration, then run:  uv run python -m tests.test_schema_reference
-- tests/test_schema_reference.py fails if this file and the migrations disagree.
--
-- Rationale for each table lives in the migration that introduced it, and in
-- docs/03-data-model.md and docs/04-daily-schedule-and-goals.md §3.
--
-- user_id is on every table except schema_version (which records what this database
-- has applied, not whose data it is) and is always 1. Do not remove it.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
"""


def render(conn: sqlite3.Connection) -> str:
    """Every DDL statement the migrations produced, in creation order."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
    ).fetchall()
    body = "\n\n".join(f"{str(row['sql']).strip()};" for row in rows)
    return f"{HEADER}\n{body}\n"


def build(tmp_path: Path) -> str:
    conn = connect(tmp_path / "reference.db")
    try:
        migrate(conn)
        return render(conn)
    finally:
        conn.close()


def test_the_reference_matches_the_migrations(tmp_path: Path) -> None:
    expected = build(tmp_path)
    actual = SCHEMA_FILE.read_text()
    assert actual == expected, (
        "specs/schema.sql is out of date with backglass/db/migrations/. "
        "Regenerate it: uv run python -m tests.test_schema_reference"
    )


def test_every_table_carries_user_id(tmp_path: Path) -> None:
    """The invariant CLAUDE.md lists as settled, asserted instead of asserted-in-prose.

    It was documented and untrue for eight tables until migration 0012. Enforcing it
    here means a new table that forgets the column fails a test rather than quietly
    making the claim false again.
    """
    conn = connect(tmp_path / "invariant.db")
    try:
        migrate(conn)
        tables = [
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        missing = [
            table
            for table in tables
            if not any(
                str(col["name"]) == "user_id"
                for col in conn.execute(f"PRAGMA table_info({table})")
            )
        ]
        # schema_version is about the database, not about an owner.
        assert missing == ["schema_version"], missing
        assert len(tables) > 20, "the reference should cover the whole schema"
    finally:
        conn.close()


if __name__ == "__main__":  # regeneration entry point, named in the header above
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        SCHEMA_FILE.write_text(build(Path(tmp)))
    print(f"wrote {SCHEMA_FILE}")
