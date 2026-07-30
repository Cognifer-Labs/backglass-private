"""SQLite connection and migrations.

Raw SQL, no ORM. docs/10 §Storage: "The schema is 13 tables and every query in the
product is a SELECT with a WHERE clause. An ORM would add a layer of indirection over
queries you can read aloud."
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
QUERIES_DIR = Path(__file__).parent / "queries"

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """The migration files and the recorded schema version disagree."""


def _dict_row(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> dict[str, object]:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the pragmas docs/10 §Storage requires on every connection.

    WAL matters because the dashboard reads while the sync writes.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = _dict_row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso() -> str:
    """UTC, second precision, with an explicit offset. Every timestamp column uses this."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# ──────────────────────────────────────────────────────────────── migrations


def _available() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise MigrationError(
                f"{path.name} does not match NNNN_lower_snake.sql; rename it or move it out"
            )
        found.append((int(match.group(1)), path))
    expected = list(range(1, len(found) + 1))
    if [version for version, _ in found] != expected:
        raise MigrationError(
            f"migration numbers must be contiguous from 0001; got {[v for v, _ in found]}"
        )
    return found


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _applied(conn: sqlite3.Connection) -> dict[int, dict[str, object]]:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if table is None:
        return {}
    rows = conn.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
    return {int(row["version"]): row for row in rows}


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied this call.

    Refuses to proceed if the file set and the recorded version disagree, per docs/10
    §Storage. Two ways they can disagree, both fatal:

      - a migration recorded as applied is missing from disk
      - a migration recorded as applied has different bytes than the file on disk

    The second is the one that bites: editing an already-applied migration leaves every
    other copy of the database on a schema this one has never seen.
    """
    available = _available()
    applied = _applied(conn)

    for version, path in available:
        record = applied.pop(version, None)
        if record is None:
            continue
        if record["filename"] != path.name:
            raise MigrationError(
                f"migration {version:04d} was applied as {record['filename']!r} "
                f"but is now {path.name!r}"
            )
        if record["checksum"] != _checksum(path):
            raise MigrationError(
                f"{path.name} has changed since it was applied. Migrations are immutable; "
                f"add a new one instead of editing this."
            )
    if applied:
        missing = ", ".join(str(v) for v in sorted(applied))
        raise MigrationError(
            f"schema_version records migration(s) {missing} that are not on disk"
        )

    done = _applied(conn)
    newly: list[int] = []
    for version, path in available:
        if version in done:
            continue
        # The BEGIN/COMMIT are inside the script rather than around it. `executescript`
        # issues an implicit COMMIT before it runs, which silently discards any
        # transaction opened out here — so an outer BEGIN would leave the migration
        # running unprotected in autocommit, and the rollback path would then fail with
        # "no transaction is active" and mask the original error.
        #
        # The version row is interpolated rather than bound because it has to be inside
        # the same script to be inside the same transaction. All three values are
        # machine-generated and cannot contain a quote: the filename is validated against
        # _MIGRATION_NAME above, the checksum is hex, the timestamp is ISO 8601.
        script = f"BEGIN;\n{path.read_text()}\nINSERT INTO schema_version "
        script += "(version, filename, checksum, applied_at) VALUES "
        script += f"({version}, '{path.name}', '{_checksum(path)}', '{now_iso()}');\nCOMMIT;"
        try:
            conn.executescript(script)
        except Exception:
            conn.executescript("ROLLBACK;")
            raise
        newly.append(version)
    return newly


# ──────────────────────────────────────────────────────────────── queries


def query(name: str) -> str:
    """Load a named .sql file from db/queries/. Named so callers read like prose."""
    path = QUERIES_DIR / f"{name}.sql"
    if not path.exists():
        raise FileNotFoundError(f"no query named {name!r} in {QUERIES_DIR}")
    return path.read_text()


def rows(
    conn: sqlite3.Connection, name: str, params: object = ()
) -> Iterator[dict[str, object]]:
    yield from conn.execute(query(name), params)  # type: ignore[arg-type]
