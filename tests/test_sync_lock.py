"""The run lock: two writing runs never overlap.

tasks/todo.md §Found and not fixed (2026-08-03): launchd fires sync every 30 minutes
and nothing stopped a manual sync from overlapping a backfill — two extraction passes
select the same pending items and extract them twice, with only the 0.85 fuzzy dedup
between that and a duplicated ledger. The lock is an advisory flock on
`<db>.sync-lock` held for the whole run; the OS releases it on process death, so a
crashed run cannot strand it.
"""

from __future__ import annotations

import fcntl

import pytest

from backglass.config import Settings
from backglass.sync import SyncLocked, run_lock, sync
from tests.conftest import FakeModel


def _lock_path(settings: Settings):
    db = settings.db_path
    return db.parent / f"{db.name}.sync-lock"


def test_sync_acquires_and_releases_the_lock(conn, settings) -> None:
    # Pass branch: a normal run completes, and afterwards the lock is free — proven
    # by taking it ourselves on a fresh descriptor, which would raise if still held.
    sync(conn, settings, [], FakeModel())
    with _lock_path(settings).open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_second_run_is_refused_while_the_first_holds_the_lock(conn, settings) -> None:
    # The concurrent process is simulated with a raw flock on a separate descriptor —
    # flock contends between open file descriptions, so this is the same shape as a
    # real launchd run overlapping a manual one. The reentrancy shortcut inside
    # run_lock cannot help here because that flag lives in the other "process".
    path = _lock_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        other.write("pid 99999 since 2026-08-05T00:00:00+00:00")
        other.flush()
        with pytest.raises(SyncLocked, match="99999"):
            sync(conn, settings, [], FakeModel())


def test_refused_run_leaves_no_run_row(conn, settings) -> None:
    # A refused run did no work and must not look like one that did: the run table
    # is what doctor and the audit read to say "the last sync was N hours ago".
    path = _lock_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SyncLocked):
            sync(conn, settings, [], FakeModel())
    assert conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"] == 0


def test_nested_acquisition_nests_instead_of_deadlocking(settings) -> None:
    # batch submit holds the lock around the sync(extract=False) it calls; the inner
    # acquisition must nest in-process, and the lock must be fully released after.
    with run_lock(settings), run_lock(settings):
        pass
    with _lock_path(settings).open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
