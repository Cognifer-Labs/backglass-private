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
import subprocess
import sys
import textwrap

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


def _another_process_can_lock(settings: Settings) -> bool:
    """Can a genuinely separate process take this lock through `run_lock`?

    Every other test in this file stands in for the second run with a hand-rolled
    `flock` on the same file. That is enough to prove the lock is *held*, and a
    mutation pass showed it is not enough to prove the lock is *exclusive*: weakening
    `LOCK_EX` to `LOCK_SH` — which is precisely "two syncs at once", the entire defect
    this file exists to prevent — leaves all four of them green, because an exclusive
    probe conflicts with a shared lock as readily as with an exclusive one.

    Only a child asking the same way the second sync would asks the real question. It
    costs about 50ms.
    """
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from backglass.config import Settings
        from backglass.sync import SyncLocked, run_lock
        settings = Settings(db_path=Path(sys.argv[1]))
        try:
            with run_lock(settings):
                print("yes")
        except SyncLocked:
            print("no")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(settings.db_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip() == "yes"


def test_a_second_process_is_refused(settings: Settings) -> None:
    """The defect itself, asked of a real second process rather than of a probe."""
    assert _another_process_can_lock(settings), "nothing should hold it yet"
    with run_lock(settings):
        assert not _another_process_can_lock(settings)


def test_a_second_process_may_have_it_afterwards(settings: Settings) -> None:
    """The other half, and the one a same-process test cannot see: a lock that was
    never released looks reentrant to the next caller in this interpreter and would
    refuse the launchd job forever without a single test noticing."""
    with run_lock(settings):
        pass
    assert _another_process_can_lock(settings)


def test_two_databases_do_not_block_each_other(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The depth is keyed by database, not counted once for the process. A single
    counter answers "am I already holding this?" for the wrong ledger as readily as the
    right one — holding one would make the first acquire on a second look reentrant and
    skip locking it entirely."""
    other = Settings(db_path=tmp_path / "other.db")
    with run_lock(settings):
        # Probed while held, not after. Asserting once both blocks have exited passes
        # against the bug: a shared counter would treat the second acquire as
        # reentrant, never lock `other` at all, and still leave both files free at the
        # end — which is the state a post-hoc assertion checks.
        assert _another_process_can_lock(other), "a second ledger must not be blocked"
        with run_lock(other):
            assert not _another_process_can_lock(other), "the second ledger IS locked"
            assert not _another_process_can_lock(settings)
    assert _another_process_can_lock(settings)
    assert _another_process_can_lock(other)
