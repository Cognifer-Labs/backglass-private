"""Snapshots of the ledger, and the way back from one. Audit item #23.

The SQLite file is the only copy of the record. docs/08 keeps `data/` out of every
cloud sync deliberately, so there is no accidental safety net underneath it — which
makes a local, scheduled, verified snapshot the whole of the backup story.

Three decisions worth stating once:

- **`VACUUM INTO`, not a file copy.** The dashboard reads while the sync writes, so the
  database is in WAL mode and almost never quiet. Copying the file mid-write yields a
  snapshot missing whatever sits in the `-wal`; `VACUUM INTO` runs inside a read
  transaction and writes a self-contained, already-checkpointed database.
- **Timestamps are UTC.** The owner moves between UTC-7 and UTC+5:30, and a local-time
  filename would sort backwards twice a year and again on every flight. Rotation reads
  these names, so they have to be monotonic.
- **Snapshots are 0600.** A snapshot is a byte-for-byte copy of the `credential` table,
  so it is exactly as sensitive as the database and inherits its mode.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# The same 0600 the live database is held at, imported rather than repeated so the two
# cannot drift: a snapshot holds the same Google tokens the original does.
from backglass.db import _DB_MODE

#: `backglass-YYYYMMDD-HHMMSS.db`, always UTC.
_STAMP = "%Y%m%d-%H%M%S"
_SNAPSHOT_NAME = re.compile(r"^backglass-(\d{8})-(\d{6})\.db$")

#: Rotation window. Seven dailies covers "I broke it last week and only noticed today";
#: four weeklies covers the slow corruption that a week of dailies would have already
#: rotated past.
DAILY_KEEP = 7
WEEKLY_KEEP = 4

#: The doctor fails past this. A daily job that has not produced a snapshot in two days
#: has missed two runs, which is a broken job rather than a closed lid.
STALE_AFTER_HOURS = 48


class BackupError(RuntimeError):
    """A snapshot could not be taken, or could not be trusted once taken."""


def _stamp_of(path: Path) -> datetime | None:
    """The UTC time encoded in a snapshot filename, or None if it is not one of ours.

    The pattern matches eight digits, which is not the same as a date: a file named
    `backglass-20260231-020000.db` (February 31st, from a hand-copy or a clock-skewed
    machine) made `strptime` raise straight out through `snapshots()` into both
    `rotate()` and `freshness()` — so one stray filename took out `doctor` entirely and
    stopped rotation forever, while `backup` kept appending. "Anything else in there is
    ignored" has to mean it, or the backup directory is a place a typo can brick.
    """
    match = _SNAPSHOT_NAME.match(path.name)
    if not match:
        return None
    try:
        parsed = datetime.strptime(f"{match.group(1)}-{match.group(2)}", _STAMP)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def snapshots(backup_dir: Path) -> list[tuple[datetime, Path]]:
    """Every snapshot in `backup_dir`, newest first. Anything else in there is ignored."""
    found = [
        (stamp, path)
        for path in backup_dir.glob("backglass-*.db")
        if (stamp := _stamp_of(path)) is not None
    ]
    return sorted(found, reverse=True)


def snapshot(db_path: Path, backup_dir: Path, *, now: datetime | None = None) -> Path:
    """Write a verified snapshot of `db_path` into `backup_dir` and return its path.

    Raises `BackupError` rather than leaving a half-written file behind: a snapshot that
    fails its own integrity check is deleted before this returns, because a corrupt file
    in the backup directory is worse than no file — it is a backup you would trust.
    """
    if not db_path.exists():
        raise BackupError(f"no database at {db_path} to snapshot")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    target = backup_dir / f"backglass-{stamp.strftime(_STAMP)}.db"
    if target.exists():
        raise BackupError(f"{target.name} already exists — one snapshot per second")

    conn = sqlite3.connect(db_path)
    try:
        # Bound, not interpolated: VACUUM INTO takes an expression, so the path goes in
        # as a parameter and no quoting question arises.
        conn.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise BackupError(f"could not snapshot {db_path}: {exc}") from exc
    finally:
        conn.close()

    # SQLite created the file under the umask, so there is a brief window at 0644 here.
    # Unavoidable — VACUUM INTO refuses to write into a file that already exists, so it
    # cannot be pre-created at 0600 the way db.connect() creates the live database.
    # Same degrade as db._restrict: a mount that cannot express the mode does not get
    # to stop the backup.
    with contextlib.suppress(OSError):
        target.chmod(_DB_MODE)

    if not verify(target):
        target.unlink(missing_ok=True)
        raise BackupError(f"snapshot of {db_path} failed its integrity check; discarded")
    return target


def verify(path: Path) -> bool:
    """True if `path` is a SQLite database that passes `PRAGMA integrity_check`.

    Read-only, so verifying a snapshot can never be the thing that damages it.
    """
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()
    return bool(row) and row[0] == "ok"


#: Tables every Backglass ledger has had since the first migration. A restore candidate
#: missing any of them is not this application's database.
_LEDGER_TABLES = frozenset({"schema_version", "source_item", "commitment", "credential"})


def is_ledger(path: Path) -> bool:
    """True if `path` looks like a Backglass ledger, not merely a valid SQLite file.

    `verify()` answers "is this a readable database"; it says nothing about *which*
    database. That gap let `restore --yes` accept any SQLite file at all — a notes
    export, a browser history, someone else's app — and replace the ledger with it.
    The safety snapshot makes that recoverable, but only for an owner who works out
    which of several identically-named `backglass-*.db` files was theirs, while every
    command dies on MigrationError in the meantime. Restoring the wrong database is not
    a mistake worth being polite about.
    """
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()
    return names >= _LEDGER_TABLES


def rotate(backup_dir: Path) -> list[Path]:
    """Delete snapshots outside the keep window. Returns what was deleted.

    Keeps the `DAILY_KEEP` most recent, then the newest survivor from each of the
    `WEEKLY_KEEP` most recent ISO weeks beyond that window. Purely a function of the
    filenames, so it is deterministic and testable without waiting a month.
    """
    if not backup_dir.exists():
        return []
    found = snapshots(backup_dir)
    keep = {path for _, path in found[:DAILY_KEEP]}

    weekly: dict[tuple[int, int], Path] = {}
    for stamp, path in found[DAILY_KEEP:]:
        year, week, _ = stamp.isocalendar()
        # found is newest-first, so the first sighting of a week is that week's newest.
        weekly.setdefault((year, week), path)
    for _, path in sorted(weekly.items(), reverse=True)[:WEEKLY_KEEP]:
        keep.add(path)

    deleted = [path for _, path in found if path not in keep]
    for path in deleted:
        path.unlink(missing_ok=True)
    return deleted


def freshness(
    backup_dir: Path, *, job_installed: bool, now: datetime | None = None
) -> tuple[str, str]:
    """The doctor's backup verdict: `("ok" | "note" | "fail", message)`.

    A fresh install has no snapshot yet and must not hard-fail before its first run —
    but it is said loudly, because "no backups at all" is the state this whole module
    exists to end. Staleness is only a failure once the launchd job is loaded; without
    it, the missing job is already the failing check and two reds for one cause is noise.
    """
    found = snapshots(backup_dir)
    if not found:
        return "note", (
            f"no ledger snapshot in {backup_dir} — run `backglass backup` now; "
            "the database is the only copy of the record"
        )
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    # A snapshot stamped in the future is not a fresh one. `snapshots()` sorts by
    # filename stamp, so a clock-skewed or hand-copied `backglass-20991231-*.db` sorts
    # first, yields a negative age, and reads as "fresh" forever — hiding a genuinely
    # stale newest-real snapshot behind a permanent green check. Ignore anything dated
    # after now, and say so if that leaves nothing.
    dated = [(stamp, path) for stamp, path in found if stamp <= moment]
    if not dated:
        newest = found[0][1].name
        return "fail", (
            f"every snapshot in {backup_dir} is dated in the future (newest {newest}) — "
            "the backup clock or the filenames are wrong, so freshness cannot be judged"
        )
    stamp, path = dated[0]
    hours = (moment - stamp).total_seconds() / 3600
    if hours > STALE_AFTER_HOURS and job_installed:
        return "fail", (
            f"newest snapshot {path.name} is {hours:.0f}h old — com.backglass.backup is "
            "loaded but not producing snapshots; check data/backup.err"
        )
    return "ok", f"{path.name}, {hours:.0f}h old"


def restore_into(snapshot_path: Path, db_path: Path) -> None:
    """Replace the database at `db_path` with `snapshot_path`.

    Atomic via `os.replace` onto a same-directory temporary, so an interrupted restore
    leaves the original database intact rather than a truncated half of one.

    The WAL sidecars are removed **before** the swap, not after. They describe pages of
    the *old* file, so between `os.replace` and their removal there is a window where
    the restored database sits beside the previous one's WAL — and any process that
    opens it in that window (the dashboard, a sync, launchd firing on schedule) recovers
    those pages onto the new file and silently undoes the restore. `integrity_check`
    passes afterwards, so nothing reports it. Removing them first means the worst case
    is a crash between the two steps, which leaves the *original* database without its
    WAL — recoverable, and loud, rather than a clean-looking wrong answer.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    staged = db_path.with_name(db_path.name + ".restoring")
    fd = os.open(staged, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, _DB_MODE)
    try:
        with open(fd, "wb") as out, snapshot_path.open("rb") as src:
            while chunk := src.read(1 << 20):
                out.write(chunk)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    os.replace(staged, db_path)
    with contextlib.suppress(OSError):
        db_path.chmod(_DB_MODE)
