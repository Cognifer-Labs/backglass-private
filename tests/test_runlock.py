"""One pipeline run at a time. The defect tasks/todo.md left open, closed.

The claim under test is not "a lock object works" — it is that two overlapping runs
cannot both extract the same pending items, which is what the launchd job firing every
thirty minutes during a manual backfill actually does. So the tests drive `sync()` and
`backglass sync`, not just `runlock.held`.

Most of these stand in for the second process with a second descriptor: `flock` is held
by the open file description rather than by the process, so a second `open()` inside one
test conflicts with the first exactly as another process would, and the tests stay fast.

That stand-in has one blind spot, and a mutation pass found it: it only ever proves that
*something else* holding the lock refuses `held()`, never that `held()` itself excludes.
Weakening `LOCK_EX` to `LOCK_SH` — which would let two syncs run at once, the entire
defect — left every one of those tests green. `test_a_second_process_is_refused` spawns a
real second process for exactly that claim, and pays 50ms for it.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from backglass import __main__ as cli
from backglass import runlock
from backglass.config import Settings
from backglass.sync import sync


class _Held:
    """Another process, standing in for one: an independent descriptor on the lock."""

    def __init__(self, db_path: Path, *, line: str = "41221 2026-08-05T12:04:00 sync\n"):
        self.path = runlock.lock_path(db_path)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, line.encode())

    def release(self) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _another_process_can_lock(db_path: Path) -> bool:
    """Can a genuinely separate process take this lock through `runlock.held`?

    Two things make the subprocess worth its 50ms, and both were found by mutating the
    module rather than by reading it:

      * A same-process descriptor cannot answer the question honestly. `runlock` would
        have to be asked twice in one interpreter, and its reentrancy counter —
        correctly — says yes to the second ask.
      * The child has to run `held` itself, not a hand-rolled `flock`. A raw `LOCK_EX`
        probe conflicts with a shared lock as readily as with an exclusive one, so
        weakening `held` to `LOCK_SH` — two syncs at once, the whole defect — passed a
        probing child. Only a child asking the same way the second sync would asks the
        real question.
    """
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from backglass import runlock
        try:
            with runlock.held(Path(sys.argv[1])):
                print("yes")
        except runlock.RunLocked:
            print("no")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(db_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip() == "yes"


class TestTheLockItself:
    def test_a_second_holder_is_refused_not_queued(self, settings: Settings) -> None:
        """Queueing would be worse than refusing: the launchd job would pile up behind a
        long backfill and run five times in a row the moment it finished."""
        other = _Held(settings.db_path)
        try:
            with pytest.raises(runlock.RunLocked) as caught, runlock.held(settings.db_path):
                pass
        finally:
            other.release()
        assert "already running" in str(caught.value)
        assert "nothing was written" in str(caught.value)

    def test_the_refusal_names_the_holder(self, settings: Settings) -> None:
        other = _Held(settings.db_path)
        try:
            with pytest.raises(runlock.RunLocked) as caught, runlock.held(settings.db_path):
                pass
        finally:
            other.release()
        assert "pid 41221" in str(caught.value)
        assert "2026-08-05T12:04:00" in str(caught.value)

    def test_an_unwritten_lock_file_still_refuses(self, settings: Settings) -> None:
        """The holder writes its line *after* taking the lock, so a reader can arrive in
        between. Refusing without detail beats reporting a nonexistent holder — and beats
        raising something the caller has never heard of."""
        other = _Held(settings.db_path, line="")
        try:
            with pytest.raises(runlock.RunLocked) as caught, runlock.held(settings.db_path):
                pass
        finally:
            other.release()
        assert "pid" not in str(caught.value)

    def test_it_is_released_when_the_holder_leaves(self, settings: Settings) -> None:
        with runlock.held(settings.db_path):
            pass
        with runlock.held(settings.db_path):
            pass  # a second acquire proves the first released

    def test_a_crash_inside_the_run_releases_it(self, settings: Settings) -> None:
        """The reason this is a file lock and not a row: the case that matters is the one
        where nothing gets to clean up. A stale row would convert an occasional duplicate
        into a permanent outage."""
        with pytest.raises(ZeroDivisionError), runlock.held(settings.db_path):
            raise ZeroDivisionError
        with runlock.held(settings.db_path):
            pass

    def test_it_is_reentrant_within_one_process(self, settings: Settings) -> None:
        """`batch submit` calls `sync()`, so one process takes the lock through two
        doors. flock cannot answer "is this me", so the depth is counted."""
        with runlock.held(settings.db_path, what="batch submit"), runlock.held(
            settings.db_path, what="sync"
        ):
            pass
        # And released once, at the end: a depth that never reaches zero would leave the
        # lock held for the life of the process and refuse every later run.
        with runlock.held(settings.db_path):
            pass

    def test_the_inner_exit_does_not_release_the_outer(self, settings: Settings) -> None:
        """The failure the reentrancy counter can easily have: the nested exit closes the
        descriptor and a third process walks in while the outer run is still writing."""
        released_early = False
        with runlock.held(settings.db_path, what="batch submit"):
            with runlock.held(settings.db_path, what="sync"):
                pass  # noqa: SIM117 - the nesting IS the thing under test
            probe = os.open(runlock.lock_path(settings.db_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                released_early = True
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                pass
            finally:
                os.close(probe)
        assert not released_early, "the outer run's lock was dropped by the inner exit"

    def test_two_databases_do_not_block_each_other(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """A demo copy and a test's tmp file are unrelated runs."""
        other_db = tmp_path / "other.db"
        with runlock.held(settings.db_path), runlock.held(other_db):
            pass

    def test_a_second_process_is_refused(self, settings: Settings) -> None:
        """The defect itself: two syncs at once. A shared lock would satisfy every other
        test in this file and still let both runs extract the same pending items."""
        assert _another_process_can_lock(settings.db_path), "nothing should hold it yet"
        with runlock.held(settings.db_path):
            assert not _another_process_can_lock(settings.db_path)

    def test_a_second_process_may_have_it_afterwards(self, settings: Settings) -> None:
        """The other half, and the one a same-process test cannot see: a `held()` that
        never released would look reentrant to the next caller in this interpreter and
        refuse the launchd job forever."""
        with runlock.held(settings.db_path):
            pass
        assert _another_process_can_lock(settings.db_path)

    def test_the_holder_leaves_its_name(self, settings: Settings) -> None:
        """`test_the_refusal_names_the_holder` writes that line itself, so it proves the
        reader and not the writer."""
        with runlock.held(settings.db_path, what="sync"):
            line = runlock.lock_path(settings.db_path).read_text()
        assert line.split(" ")[0] == str(os.getpid())
        assert line.strip().endswith("sync")

    def test_the_lock_file_is_not_world_readable(self, settings: Settings) -> None:
        with runlock.held(settings.db_path):
            mode = runlock.lock_path(settings.db_path).stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)


class TestTheDoors:
    def test_a_second_sync_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The whole point: not that the second run fails, but that it never reaches the
        stage where it would select the same `pending_extraction` rows."""
        before = conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]
        other = _Held(settings.db_path)
        try:
            with pytest.raises(runlock.RunLocked):
                sync(conn, settings, [], _NeverCalled())
        finally:
            other.release()
        after = conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]
        assert after == before, "a refused run must not even record itself"

    def test_a_dry_run_is_never_refused(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """It writes nothing, so it can neither corrupt a running sync nor be corrupted
        by one — and refusing it would remove the one command that is always safe."""
        other = _Held(settings.db_path)
        try:
            report = sync(conn, settings, [], _NeverCalled(), dry_run=True)
        finally:
            other.release()
        assert report.fetched == 0

    def test_the_cli_says_it_plainly(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A refused run is the program declining to be the second writer, not a crash.
        Driven through `main()` — the console script — because that is where the
        sentence lives; a command that raised through `app` alone would traceback."""
        del conn
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        monkeypatch.setattr(cli, "_all_connectors", lambda *a, **k: [_NeverCalled()])
        monkeypatch.setattr(cli, "_build_model_client", lambda *a, **k: _NeverCalled())
        monkeypatch.setattr(cli, "_contacts_source", lambda *a, **k: None)
        monkeypatch.setattr(cli.sys, "argv", ["backglass", "sync"])
        other = _Held(settings.db_path)
        try:
            with pytest.raises(SystemExit) as caught:
                cli.main()
        finally:
            other.release()
        assert caught.value.code == 1
        printed = capsys.readouterr()
        assert "already running" in printed.err
        assert "pid 41221" in printed.err
        assert "Traceback" not in printed.err

    def test_batch_collect_is_locked_too(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The half that inserts the commitments. Two collectors reading one finished
        batch write every extraction twice — the same duplication, a different door."""
        from backglass import batch

        other = _Held(settings.db_path)
        try:
            with pytest.raises(runlock.RunLocked):
                batch.collect(conn, settings, _NeverCalled())
        finally:
            other.release()

    def test_an_ordinary_batch_collect_still_runs(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass import batch

        report = batch.collect(conn, settings, _NeverCalled())
        assert report.batches == 0  # nothing outstanding, and nothing raised

    def test_an_ordinary_sync_still_runs(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A lock that refuses everything would pass every test above."""
        before = conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]
        sync(conn, settings, [], _NeverCalled())
        after = conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]
        assert after == before + 1


class _NeverCalled:
    """A model client the pipeline has no items to call. `sync` with no connectors
    fetches nothing, so nothing reaches the model — asserted by every attribute raising."""

    spend_is_imputed = True

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the model was called ({name}) on a run with no sources")


def test_the_console_script_points_at_the_handler() -> None:
    """The sentence only reaches the owner if the installed command runs `main`.
    `pyproject.toml` pointing back at `app` would silently restore the traceback."""
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert 'backglass = "backglass.__main__:main"' in text
