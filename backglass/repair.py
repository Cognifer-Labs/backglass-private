"""The repair loop, as one thing that can be called rather than a CLI epilogue.

Everything here already ran — as ninety lines of `try: … except Exception: pass` inside
`__main__.py`'s sync command, and nowhere else. That had three consequences worth naming,
because they are the reason this module exists:

**It only ran from one entry point.** The 30-minute launchd sync got the whole chain; an
app opened at 09:00 got `catchup` alone. Nothing else — a web action, a manual `plan`, a
future trigger — repaired anything at all. The audit's phrase for it is exact: the loop was
an epilogue, not a reconciler.

Named `repair`, not `reconcile`: `reconcile` already means something else here — asking
whether a source is still collecting, by counting the store against the ledger
(`retraction.py`, `_check_reconciles`, `tests/test_reconcile.py`). Two unrelated things
under one word in one codebase is how a reader ends up in the wrong file.

**Its failures were silent.** Seven bare `except Exception: pass` blocks. Rule 5 says a
failing part degrades, is *logged*, is *surfaced*, and the run exits non-zero; those
swallowed. If the logic checker started raising on every pass, the ledger would quietly
stop being repaired and every surface would look healthy.

**It could not be tested.** Inline in a Typer command, seven steps deep, it had no name to
call and no report to assert on.

So: an ordered list of steps, each best-effort but *recorded*, returning what it did and
what failed. The order is not incidental and is preserved exactly from the CLI —

  1. `catchup`   fills a hole (the plan or brief a slept-through 05:45 never produced)
  2. `replan`    refreshes a plan the day has moved under; proposed only, never accepted
  3. `logic`     throws out what the record already contradicts, *before* anything asks
  4. `questions` asks what is left, having been spared the mooted ones
  5. `notify`    says what the day demands, inside the owner's window
  6. `situation` renders the state doc, after the repairs above have had their say
  7. `vault`     writes the ledger out as markdown — last, because it reports on the rest

Errors are returned, never raised and never printed here: the sync command folds them into
its own report so they reach `run.errors_json` and the Sources panel, which is where rule 5
says a failure belongs. `on_open` prints them, because a dashboard has no run row to carry
them.

**No `run` row is written for a repair pass**, deliberately. Four readers take the newest
row with no `kind` filter (`state`, `panels`, `brief/daily`, `doctor`), so a non-sync row
would immediately be reported as the last sync — a confident answer assembled from the
wrong input, which is the failure `state` exists to prevent. The sync's own row carries the
errors instead.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from backglass.config import Settings


@dataclass(frozen=True)
class Step:
    """One repair, and the name it fails under."""

    name: str
    run: Callable[[sqlite3.Connection, Settings, date], list[str]]


@dataclass
class Report:
    #: What happened, in the words the CLI already printed.
    lines: list[str] = field(default_factory=list)
    #: One per step that raised, prefixed with the step's name.
    errors: list[str] = field(default_factory=list)
    #: Which steps ran to completion, in order — the thing a test asserts on.
    ran: list[str] = field(default_factory=list)
    #: True when another process held the sync lock. Not an error: the other run is doing
    #: this work, which is the same conclusion `sync` reaches (`SyncLocked`).
    skipped: bool = False


def _catchup(conn: sqlite3.Connection, settings: Settings, day: date) -> list[str]:
    """The net under the morning jobs — the only step that can cost model calls.

    This is the one job that runs on wake, so on a laptop that slept through 05:45 it is
    what notices the plan and the brief were never produced. It fills a hole and never
    replaces anything.
    """
    del day
    from backglass import catchup

    return [
        f"caught up {produced.surface} for {produced.day}: {produced.detail}"
        for produced in catchup.run(conn, settings)
    ]


def _replan(conn: sqlite3.Connection, settings: Settings, day: date) -> list[str]:
    """The mirror of the net: catchup fills the plan that is missing, replan refreshes the
    plan that exists when the day has changed under it. Proposed plans are regenerated;
    accepted plans only get a knock."""
    del day
    from backglass.plan import replan as replan_mod

    replanned = replan_mod.run(conn, settings)
    if replanned is None:
        return []
    return [f"plan {replanned.action} for {replanned.day}: {replanned.detail}"]


def _logic(conn: sqlite3.Connection, settings: Settings, day: date) -> list[str]:
    """Before asking anything, throw out what the record already contradicts.

    Ahead of detection on purpose — a question mooted here is one the owner never has to
    read, and a commitment resolved here is one nothing re-asks about.
    """
    from backglass import logic as logic_mod

    disposed = logic_mod.run(conn, settings, day)
    out: list[str] = []
    if disposed.applied:
        out.append(
            f"logic check disposed of {disposed.applied} item(s): "
            + ", ".join(f"{rule} ×{n}" for rule, n in disposed.by_rule().items())
        )
    # The checker's own per-rule failures, which its caller used to drop on the floor.
    out.extend(f"logic: {err}" for err in disposed.errors)
    return out


def _questions(conn: sqlite3.Connection, settings: Settings, day: date) -> list[str]:
    """Auto-recognition runs where the data arrives, not only when the owner opens /ask."""
    from backglass import questions as questions_mod

    asked = questions_mod.refresh(conn, settings, day)
    return [f"{asked} new question(s) for you — answer at /ask"] if asked else []


def _notify(conn: sqlite3.Connection, settings: Settings, day: date) -> list[str]:
    """And say what the day demands, inside the owner's notify window."""
    del day
    from backglass import notify as notify_mod

    return [
        f"notified: {note.title} ({note.delivered})"
        for note in notify_mod.run(conn, settings)
    ]


def _situation(conn: sqlite3.Connection, settings: Settings, day: date) -> list[str]:
    """The state doc, after the repairs above and before the vault reads it.

    What the checker disposed of and what the replan moved are part of the owner's
    situation; a doc rendered ahead of them would describe the ledger as it was at the top
    of the run. A version is stored only when the body differs, so an unchanged ledger
    writes nothing here.
    """
    from backglass import situation as situation_mod

    version, moved = situation_mod.refresh(conn, settings, day)
    if version is None:
        return []
    return [f"situation v{version.version_id}: {len(moved)} line(s) changed"]


def _vault(conn: sqlite3.Connection, settings: Settings, day: date) -> list[str]:
    """Last, because it is a report over everything the steps above just changed.

    A vault on an unplugged drive is not a sync failure — but it is now a *reported* one
    rather than a silent one.
    """
    del day
    from pathlib import Path

    from backglass import vault as vault_mod
    from backglass.plan import timezones

    if not settings.vault_export_path:
        return []
    report = vault_mod.export(
        conn,
        settings,
        root=Path(settings.vault_export_path).expanduser(),
        now=timezones.local_now(settings),
    )
    return [report.line()] if (report.written or report.failed) else []


#: The chain, in order. Reordering this changes behaviour — see the module docstring for
#: what each position is buying.
STEPS: tuple[Step, ...] = (
    Step("catchup", _catchup),
    Step("replan", _replan),
    Step("logic", _logic),
    Step("questions", _questions),
    Step("notify", _notify),
    Step("situation", _situation),
    Step("vault", _vault),
)


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    day: date | None = None,
    steps: tuple[Step, ...] | None = None,
) -> Report:
    """Every step, each failing on its own. Writes through `conn`; the caller commits.

    Rule 5's unit is the step: one repair that raises must not cost the other six. The
    difference from the code this replaces is that the failure is *returned* instead of
    passed over, so a checker that has been broken for a week is visible rather than
    inferred from a board that stopped changing.
    """
    from backglass.plan import timezones

    if day is None:
        day = timezones.local_now(settings).date()

    report = Report()
    for step in steps if steps is not None else STEPS:
        try:
            report.lines.extend(step.run(conn, settings, day))
            report.ran.append(step.name)
        except Exception as exc:  # noqa: BLE001 — rule 5, per step
            report.errors.append(f"{step.name}: {type(exc).__name__}: {exc}")
    return report


def on_open(settings: Settings, *, now: datetime | None = None) -> Report:
    """The whole chain for an app that has just been opened, on its own connection.

    Takes the sync lock, for `catchup.on_open`'s reason: an app opened at :18 while the
    30-minute sync is mid-run would otherwise produce a second proposal for the same day,
    one of them immediately superseded and both of them paid for. A held lock means
    another run is already doing this work, so the answer is to skip.

    Widened from catch-up alone on 2026-08-24. Everything after `catchup` in the chain is
    deterministic and cheap — no model calls — so an app that is open is now an app that
    repairs, rather than one that waits up to half an hour for launchd to do it.
    """
    from backglass.db import connect
    from backglass.plan import timezones
    from backglass.sync import SyncLocked, run_lock

    day = (now or timezones.local_now(settings)).date()
    try:
        with run_lock(settings):
            conn = connect(settings.db_path)
            try:
                report = run(conn, settings, day=day)
                conn.commit()
            finally:
                conn.close()
    except SyncLocked:
        return Report(skipped=True)
    return report


#: How stale the loop must be before opening the app is itself a reason to run it. The
#: scheduled sync fires every 30 minutes; 45 leaves slack for a long run without making a
#: healthy installation repair twice. The number exists because launchd cannot be trusted
#: to have fired: on 2026-08-17 the morning jobs had been 12h30 late for weeks, because
#: the agent that evaluates calendar intervals holds the timezone the machine booted in.
OVERDUE_MINUTES = 45


def is_overdue(
    conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None
) -> bool:
    """Has the repair loop gone longer than it should without running?

    One indexed read, because it is called on a full-page load. `kind = 'sync'` on purpose:
    the newest row of any other kind would answer a different question, and only sync rows
    carry the loop.

    A ledger that has never synced is *not* overdue — there is nothing to repair yet, and a
    fresh install should not start a planner on its first page view.
    """
    from backglass.ledger import USER_ID
    from backglass.plan import timezones

    row = conn.execute(
        "SELECT finished_at FROM run WHERE user_id = ? AND kind = 'sync'"
        " AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (USER_ID,),
    ).fetchone()
    if row is None or not row["finished_at"]:
        return False
    try:
        finished = datetime.fromisoformat(str(row["finished_at"]))
    except ValueError:
        return False
    current = now or timezones.local_now(settings)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=current.tzinfo)
    return (current - finished).total_seconds() > OVERDUE_MINUTES * 60


_running = threading.Semaphore(1)

#: The last pass this process ran on open, and when. `on_open` has nowhere to put its
#: report — the module docstring says so, and says why: a repair pass writes no `run` row
#: on purpose, because four readers take the newest row with no `kind` filter and would
#: report it as the last sync.
#:
#: So it printed to stderr, and in the desktop shell stderr is nowhere. That made rule 5
#: half-true on this path: a failing repair degraded and was logged, and was never
#: surfaced. This slot is the surface, and it is deliberately in memory rather than in a
#: table — the question is "what did the loop do since this app opened", which a fresh
#: process should answer with silence rather than with last week's pass.
_last: tuple[datetime, Report] | None = None
_last_lock = threading.Lock()


def last() -> tuple[datetime, Report] | None:
    """The most recent on-open pass in this process, or None if it has not run.

    None is the honest answer for a just-started process and must not be read as health:
    nothing has been repaired yet, which is different from nothing needing repair.
    """
    with _last_lock:
        return _last


def _record(report: Report, *, now: datetime) -> None:
    global _last
    with _last_lock:
        _last = (now, report)


def spawn_on_open(settings: Settings) -> None:
    """Run the loop on a daemon thread and return immediately.

    Same contract `catchup.spawn_on_open` has held since the dashboard first had a net,
    and for the same two reasons. The dashboard is what the desktop shell opens, so
    anything synchronous here is time the owner spends looking at a window that has not
    painted. And by rule 5 a failure to repair must degrade: the page still renders, the
    ledger is still correct, and the sidebar still says what is missing.

    Daemon, so quitting the app never waits on it. Anything half-written is a proposal the
    next open regenerates, never a partial commit — the connection commits once, at the end.
    """
    if not _running.acquire(blocking=False):
        return

    def work() -> None:
        try:
            report = on_open(settings)
            # Recorded before it is printed: stderr is a terminal's channel and the app
            # has no terminal, so the slot is the one the dashboard reads.
            from backglass.plan import timezones

            _record(report, now=timezones.local_now(settings))
            for line in report.lines:
                print(line, file=sys.stderr)
            for error in report.errors:
                print(f"repair {error}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — rule 5: never take the dashboard down
            print(f"repair loop on open failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            _running.release()

    threading.Thread(target=work, name="backglass-repair", daemon=True).start()
