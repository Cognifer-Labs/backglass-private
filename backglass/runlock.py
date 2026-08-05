"""One pipeline run at a time, per database.

The defect this closes was written up in tasks/todo.md §Found and not fixed and worked
around by unloading the launchd job:

    Two syncs can run at once. There is no lock: not a file lock, not a row, not a
    check of the `run` table for an unfinished row. The launchd job fires every 30
    minutes and a manual `backglass sync` during a backfill will overlap it, at which
    point both processes select the same `pending_extraction` rows and extract them
    twice.

The ledger's immutability trigger cannot catch it, because two extractions of one item
are two legitimate-looking commitment inserts. The only thing between that and a
duplicated ledger is the 0.85 fuzzy dedup, which is a similarity heuristic and not a
guarantee — and the duplicates it misses are exactly the ones a person then has to find
by hand.

Why an advisory file lock rather than a row
-------------------------------------------
A row saying "a run is in progress" has to be cleared, and the case that matters most is
the one where nothing gets to clear it: a machine that sleeps mid-sync, a `kill -9`, a
crash in a connector. A stale row then blocks every future run until someone finds and
deletes it, which converts an occasional duplicate into a permanent outage — a strictly
worse failure. `flock` is held by the open file description, so the kernel drops it when
the process dies however it dies. There is nothing to clean up and nothing to reset.

The lock is per database file, not per machine: two ledgers (the owner's and a demo
copy, or a test's tmp file) are unrelated runs and must not block each other.
"""

from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RunLocked(RuntimeError):
    """Another run holds the lock. Carries what little the holder left behind."""


def lock_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".lock")


#: Reentrancy, per lock file. `batch submit` calls `sync()`, so the same process takes
#: the lock twice through two different doors; without this the inner acquire would meet
#: the outer one's `flock` and refuse — flock excludes by open file description, not by
#: process, so "it is me" is not a question the kernel can answer. Depth is counted here
#: instead, and the descriptor is released only when the outermost holder leaves.
_held: dict[str, list[Any]] = {}


@contextmanager
def held(db_path: Path, *, what: str = "run") -> Iterator[None]:
    """Hold the pipeline lock for this database, or raise `RunLocked`.

    Reentrant within one process, exclusive between processes.
    """
    key = str(db_path.resolve())
    entry = _held.get(key)
    if entry is not None:
        entry[1] += 1
        try:
            yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                _release(key)
        return

    path = lock_path(db_path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        holder = _describe(fd)
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EACCES):
            raise RunLocked(
                f"another backglass {what} is already running{holder}. "
                "This one did nothing; nothing was written."
            ) from exc
        raise

    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()} {datetime.now(UTC).isoformat()} {what}\n".encode())
    os.fsync(fd)
    _held[key] = [fd, 1]
    try:
        yield
    finally:
        entry = _held.get(key)
        if entry is not None:
            entry[1] -= 1
            if entry[1] == 0:
                _release(key)


def _release(key: str) -> None:
    entry = _held.pop(key, None)
    if entry is None:
        return
    fd = entry[0]
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _describe(fd: int) -> str:
    """"(pid 41221, started 12:04)", or nothing at all.

    Best effort by construction: the holder writes its line after taking the lock, so a
    reader arriving in that window sees an empty file. An unhelpful message is better
    than a refusal to report, so the empty case degrades to no detail rather than to an
    exception of its own.
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        line = os.read(fd, 200).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
    parts = line.split(" ")
    if len(parts) < 2 or not parts[0].isdigit():
        return ""
    return f" (pid {parts[0]}, started {parts[1]})"
