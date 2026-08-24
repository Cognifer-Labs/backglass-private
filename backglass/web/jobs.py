"""One command at a time, on a thread, with something to show while it runs.

These commands take minutes. `loop` generates a plan and a brief; `duplicates` is a
3.7-second pairwise sweep; `sync` talks to a model backend over the network. A request
thread must never wait on any of them, so the route starts a job and returns, and the page
polls.

Why a job rather than "just call it and render the result": a browser that waits sixty
seconds for a response shows the owner nothing and then, on many setups, gives up before
the work does — leaving a run that finished with nobody to tell. The job survives the
request that started it, which is the property that matters.

**In memory, deliberately.** What a run *did* is already in the ledger — `loop_pass` rows,
a `run` row, `decision` rows — written by the command itself with provenance. This holds
only the transcript of a run in progress, which is worth nothing once it is over and its
effects are recorded. A table for it would be a second, weaker record of facts the ledger
already carries properly, and a migration to store what amounts to a progress bar.

**One at a time, and the guard is honesty rather than safety.** `run_lock` already makes a
second concurrent run skip instead of corrupt. What it does not do is tell the owner why
their click appeared to do nothing, so this refuses the second start and says so.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backglass.config import Settings
from backglass.db import now_iso

if TYPE_CHECKING:
    from backglass.web.commands import Command

#: Statuses a job can be in, as the page renders them.
RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass
class Job:
    key: str
    label: str
    status: str = RUNNING
    lines: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    #: False when the command itself reported bad news — a degraded sync, a failing state
    #: verdict. Distinct from `status`, which is about whether the command *ran*. Merging
    #: them would let a successful report of a problem render as a crash, or a crash
    #: render as a clean run, depending on which way the merge went.
    ok: bool = True

    @property
    def running(self) -> bool:
        return self.status == RUNNING


class Runner:
    """The one job slot, and the thread behind it.

    Instance rather than module state so a test gets its own and the dashboard gets one
    per process. Two dashboards against one ledger are still excluded by `run_lock`; this
    is about not lying to whichever owner clicked.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: Job | None = None

    @property
    def current(self) -> Job | None:
        with self._lock:
            return self._job

    def start(self, command: Command, settings: Settings) -> tuple[Job | None, str | None]:
        """Begin a run. Returns (job, refusal) — exactly one of them is set.

        Refused rather than queued when something is already running. A queue would let a
        distracted owner stack six syncs behind one, each of which will skip on the lock
        anyway; the honest answer is to say what is running and let them wait for it.
        """
        with self._lock:
            if self._job is not None and self._job.running:
                return None, f"{self._job.label} is still running"
            job = Job(key=command.key, label=command.label)
            self._job = job

        threading.Thread(
            target=self._work, args=(command, settings, job),
            name=f"backglass-cmd-{command.key}", daemon=True,
        ).start()
        return job, None

    def _work(self, command: Command, settings: Settings, job: Job) -> None:
        """Run it on its own connection, and never let it take the dashboard down.

        Its own connection because this is a thread the request path started and the
        page's connection belongs to a request that has already returned. Autocommit is
        the ledger's default, so there is no transaction to lose when the thread ends.
        """
        from backglass.db import connect

        conn: sqlite3.Connection | None = None
        try:
            conn = connect(settings.db_path)
            result = command.fn(conn, settings)
            conn.commit()
            job.lines = list(result.lines)
            job.ok = result.ok
            job.status = DONE
        except Exception as exc:  # noqa: BLE001 — rule 5: the page renders regardless
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = FAILED
            job.ok = False
        finally:
            if conn is not None:
                conn.close()
            job.finished_at = now_iso()
