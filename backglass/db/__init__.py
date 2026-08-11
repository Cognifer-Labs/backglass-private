"""SQLite connection and migrations.

Raw SQL, no ORM. docs/10 §Storage: "The schema is 13 tables and every query in the
product is a SELECT with a WHERE clause. An ORM would add a layer of indirection over
queries you can read aloud."
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
QUERIES_DIR = Path(__file__).parent / "queries"

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

#: docs/08 §General handling: "Tokens never appear in logs, in the SQLite file outside
#: the `credential` table, or in brief output." The `credential` table is inside this
#: file, so the file *is* the Google access- and refresh-token store; under the default
#: umask it is created 0644 and every other account and process on the machine can read
#: those tokens straight out of it.
_DB_MODE = 0o600

#: Warned-about paths, so a filesystem that cannot hold the mode says so once instead of
#: on every connect. Same shape as the model-backend fallback notice in extract/client.py.
_mode_warned: set[str] = set()

#: How long a writer waits for another writer before giving up.
#:
#: WAL lets the dashboard read while the sync writes, but it does not let two writers
#: overlap: the second one gets `database is locked` the moment the first holds the
#: write lock. Python's default is five seconds, and five seconds is not enough here —
#: the sync runs every half hour for one to four minutes, taking a short BEGIN IMMEDIATE
#: per extracted item, so a Resolve clicked inside that window queues behind a stream of
#: them and can lose the race repeatedly. What the owner saw was a button that hung and
#: then changed nothing, because `sqlite3.OperationalError` is not `ActionError`: it went
#: past the route's handler as a 500 and the write was simply gone.
#:
#: Thirty seconds is longer than any transaction this codebase opens (every one of them
#: is a handful of INSERTs with no model call inside — see sync.py's per-item comment),
#: so it can only ever be spent waiting on contention, never on one slow statement.
BUSY_TIMEOUT_MS = 30_000


class MigrationError(RuntimeError):
    """The migration files and the recorded schema version disagree."""


def _dict_row(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> dict[str, object]:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def _restrict(path: Path) -> None:
    """Make `path` owner-only, if it exists and is not already.

    This restricts a database it did not create, deliberately: a file left at 0644 by an
    earlier run is holding live Google tokens today, and declining to touch it would
    preserve exactly the exposure the mode exists to close. Nothing about the owner's own
    access changes — 0600 is still read/write for them.

    CLAUDE.md rule 5, applied to the filesystem instead of a source: a mount that cannot
    express the mode degrades rather than blocking the whole product, but it is said out
    loud, because a chmod that quietly did nothing is indistinguishable from one that
    worked.
    """
    try:
        if stat.S_IMODE(path.stat().st_mode) != _DB_MODE:
            path.chmod(_DB_MODE)
    except FileNotFoundError:
        return  # WAL sidecars only exist while some connection holds them.
    except OSError as exc:
        if str(path) not in _mode_warned:
            _mode_warned.add(str(path))
            print(
                f"cannot restrict {path} to {_DB_MODE:04o} ({exc}) — docs/08: the "
                f"credential table in it is readable by other accounts on this machine",
                file=sys.stderr,
            )


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the pragmas docs/10 §Storage requires on every connection.

    WAL matters because the dashboard reads while the sync writes.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Created here rather than left to SQLite so the mode is right *before* the first
        # token is written, not a chmod behind it: a create-then-chmod leaves a window in
        # which the file is world-readable. A zero-length file is a valid empty database.
        os.close(os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _DB_MODE))
    except FileExistsError:
        pass
    except OSError:
        pass  # Not ours to report: sqlite3.connect gives the real reason a line below.
    _restrict(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = _dict_row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # The -wal holds committed pages not yet checkpointed back, so it carries the same
    # rows — including `credential` — as the database itself. SQLite creates both
    # sidecars under the umask when WAL mode is entered, so they are restricted here
    # rather than at creation.
    _restrict(db_path.with_name(db_path.name + "-wal"))
    _restrict(db_path.with_name(db_path.name + "-shm"))
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
