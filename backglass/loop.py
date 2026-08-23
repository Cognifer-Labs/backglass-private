"""The passes that run after the pipeline, in one place, with one contract.

Five things happen after `sync()` returns: the morning surfaces are caught up, a drifted
plan is replanned, the logic checker disposes of what the record contradicts, the
detectors ask what is left, and the day's notifications go out. Until this module they
lived as a hundred lines of `try/except: pass` inside `sync_command`, and that shape had
four consequences nobody could see:

  - **Two entry points ran two different loops.** The CLI ran all five. The app-open
    trigger (`catchup.spawn_on_open`) ran catchup and nothing else, so opening the
    dashboard got a third of the machinery. There was no list anywhere to compare them
    against, because the list *was* the CLI function body.
  - **`except Exception: pass` is not rule 5.** The rule is log, surface, continue, exit
    non-zero. A pass throwing for a week produced no row, no panel, no exit code and no
    field in `backglass state` — indistinguishable from a pass with nothing to do. A loop
    you cannot see failing is unattended rather than autonomous.
  - **The lock covered one pass of five.** `catchup.run` was called inside `run_lock`;
    `replan`, which *supersedes day plans*, was not. An app-open catchup proposing a plan
    and a sync replanning it could interleave with nothing between them.
  - **Nothing was gated.** Forty-eight syncs a day each ran five full sweeps whether or
    not a single row had been written since the last one.

So: a declared registry, and a runner that owns the lock. The registry is the list the
two callers were missing, and because a pass declares what drives it — a clock, the data,
or neither — the gating question has somewhere to live rather than being re-decided at
each call site.

**This is not a scheduler.** launchd owns cadence (CLAUDE.md's decisions table) and every
entry point here is one-shot: it runs whatever is owed *now*, once, and returns. The name
invites a `while True: sleep(1800)`, which would re-open a closed decision.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID

#: What decides whether a pass has work. Declared per pass so the gate lives with the
#: thing it gates, and so increment 5 has one place to put the skip.
#:
#: - ``clock``: owed at an hour. Evaluates on every run *regardless of writes* — a sync
#:   that ingested nothing still crosses 06:00, and a plan is still missing at 09:00.
#: - ``data``: reads the ledger and acts on what changed. Nothing written since the last
#:   successful run means nothing to find.
#: - ``always``: neither; it must be asked every time.
CLOCK = "clock"
DATA = "data"
ALWAYS = "always"

class Skip(Exception):
    """A pass declining to run, with the reason it declined.

    Distinct from a failure and from a quiet success, because it means something neither
    of them does: there was work this pass could have done and it decided the cost was
    not owed yet. Only `duplicates` raises it today — its clustering is O(n²) over the
    open set and costs ~3.7 s, which is not a per-half-hour price for a review queue.

    A pass that is merely *cheap and idle* must not raise this. "Ran and found nothing" is
    the healthy state the record exists to prove, and dressing it as a skip would make a
    live pass indistinguishable from a gated one.
    """


#: A pass ran and did whatever it had to do — including nothing, which is the normal case.
OK = "ok"
#: It raised. Rule 5: the loop continues, and increment 2 makes this visible and costly.
FAILED = "failed"
#: It was not attempted — another run holds the lock, or its gate said there was nothing.
SKIPPED = "skipped"


@dataclass(frozen=True)
class Outcome:
    """What one pass did, in the form both a terminal and a row need.

    `lines` is what the CLI prints, and it is empty on the quiet path: a loop that
    narrates "nothing to do" five times per half-hour trains its reader to stop looking,
    which is the failure the heartbeat exists to prevent.
    """

    name: str
    status: str
    lines: tuple[str, ...] = ()
    error: str | None = None

    @property
    def quiet(self) -> bool:
        return not self.lines and self.error is None


#: The signature every pass presents to the runner: the connection, the settings, and the
#: owner's local `now` — resolved once by the runner so five passes cannot disagree about
#: what day it is mid-loop. Returns the lines it wants reported.
PassFn = Callable[[sqlite3.Connection, Settings, datetime], list[str]]


@dataclass(frozen=True)
class Pass:
    name: str
    trigger: str
    #: Whether this pass can reach the model backend, and so the spend cap. All five are
    #: currently deterministic — `logic.py` and `questions.py` hold no `ModelClient`
    #: reference at all — except catchup, which generates the plan and the brief.
    spends: bool
    fn: PassFn = field(repr=False)


def _catchup(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[str]:
    from backglass import catchup

    return [
        f"caught up {produced.surface} for {produced.day}: {produced.detail}"
        for produced in catchup.run(conn, settings, now=now)
    ]


def _replan(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[str]:
    from backglass.plan import replan as replan_mod

    replanned = replan_mod.run(conn, settings, now=now)
    if replanned is None:
        return []
    return [f"plan {replanned.action} for {replanned.day}: {replanned.detail}"]


def _logic(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[str]:
    from backglass import logic as logic_mod

    disposed = logic_mod.run(conn, settings, now.date())
    if not disposed.applied:
        return []
    by_rule = ", ".join(f"{rule} ×{n}" for rule, n in disposed.by_rule().items())
    return [f"logic check disposed of {disposed.applied} item(s): {by_rule}"]


def _questions(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[str]:
    from backglass import questions as questions_mod

    asked = questions_mod.refresh(conn, settings, now.date())
    if not asked:
        return []
    return [f"{asked} new question(s) for you — answer at /ask"]


def _notify(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[str]:
    from backglass import notify as notify_mod

    return [
        f"notified: {note.title} ({note.delivered})"
        for note in notify_mod.run(conn, settings, now=now)
    ]


def _duplicates(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[str]:
    """Clusters of open commitments that look like one promise, raised as cards.

    Gated to once per owner-local day, and the gate is the reason this is a pass of its
    own rather than another entry in `questions.detect`. Measured on a copy of the live
    ledger: `duplicates.clusters` costs **3.7 s** over 386 open commitments — 51,443
    difflib ratios and 23,871 cosine comparisons — and no index touches it, because the
    cost is the pairwise comparison itself. Running that every thirty minutes to re-derive
    a queue whose cards persist until answered would be the loop's largest expense by an
    order of magnitude, spent almost entirely on re-finding what it found at 06:00.

    Daily is the right cadence rather than a compromise: a card stays until the owner
    answers it, `DUP_BATCH_LIMIT` bounds how many are raised at once, and a duplicate
    created at 11:00 is not a thing the owner needed to be asked about by 11:30.

    The gate reads `loop_pass`, which increment 2 already writes — no second bookkeeping
    table, and the gate is visible in the same record that proves the pass is alive.
    """
    from backglass import duplicates as dup_mod
    from backglass import questions as questions_mod

    if _ran_ok_today(conn, "duplicates", now):
        raise Skip("already ran today; the clustering costs ~3.7s and the cards persist")

    asked = questions_mod.record(conn, dup_mod.questions_for(conn))
    if not asked:
        return []
    return [f"{asked} duplicate cluster(s) to settle — /ask"]


def _noise(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[str]:
    """Senders that have cost the expensive pass a lot and produced nothing, as cards.

    Gated daily for the same shape of reason as `duplicates`, at a tenth of the cost:
    `noise.candidates` mines the whole triage history and takes ~1.2 s on the live ledger,
    which is not a per-half-hour price for a queue whose cards persist until answered.

    This is the alternative to `noise_auto_promote`, and it is why that setting stays off.
    The flag saves the owner a click and makes "why did I stop seeing mail from my
    landlord" unanswerable; a card costs one click and leaves a decision row naming who
    decided. Twenty-one senders are waiting on the live ledger and none of them has ever
    been reachable except by typing a command.
    """
    from backglass import questions as questions_mod
    from backglass.extract import noise as noise_mod

    if _ran_ok_today(conn, "noise", now):
        raise Skip("already ran today; mining the triage history costs ~1.2s")

    asked = questions_mod.record(conn, noise_mod.questions_for(conn, settings))
    if not asked:
        return []
    return [f"{asked} sender(s) to decide about — /ask"]


def _ran_ok_today(conn: sqlite3.Connection, name: str, now: datetime) -> bool:
    """Has this pass already succeeded on the owner's current local day?

    Reads `local_date`, never `finished_at`. The two are different clocks on purpose:
    the timestamps are wall-clock UTC so "how long did this take" stays answerable, and
    the day is the owner's. Gating on the timestamps was the first version of this and it
    was wrong in both directions — every run looked like today because `finished_at` is
    stamped from the wall clock regardless of the day being processed, and even with a
    frozen clock 20:00 Phoenix is already tomorrow in UTC, so the gate would reopen at
    dinner. `notification.local_date` exists for the same reason.
    """
    row = conn.execute(
        "SELECT 1 FROM loop_pass WHERE user_id = ? AND name = ? AND status = ?"
        "   AND local_date = ? LIMIT 1",
        (USER_ID, name, OK, now.date().isoformat()),
    ).fetchone()
    return row is not None


#: The loop, in the order it has always run. The order is not arbitrary and moving an
#: entry is a decision, not a tidy-up:
#:
#:   1. catchup first, because everything after it reads the plan it may have just built.
#:   2. replan second — catchup fills the plan that is *missing*, replan refreshes the
#:      plan that exists and has drifted. Neither is the other's fallback.
#:   3. logic before questions, deliberately: a question mooted by the checker is one the
#:      owner never has to read, and an obligation resolved here is one nothing re-asks
#:      about. Detection after disposal, never the reverse.
#:   4. questions.
#:   5. duplicates after questions rather than inside `detect`, because it is gated to
#:      once a day and the others are not — see `_duplicates` for the measurement that
#:      made the gate necessary.
#:   6. noise, beside duplicates: another queue that existed only behind a command, and
#:      gated the same way for the same reason.
#:   7. notify last, so it can speak about anything the passes above just produced.
PASSES: tuple[Pass, ...] = (
    Pass("catchup", CLOCK, spends=True, fn=_catchup),
    Pass("replan", CLOCK, spends=True, fn=_replan),
    Pass("logic", DATA, spends=False, fn=_logic),
    Pass("questions", DATA, spends=False, fn=_questions),
    Pass("duplicates", DATA, spends=False, fn=_duplicates),
    Pass("noise", DATA, spends=False, fn=_noise),
    Pass("notify", CLOCK, spends=False, fn=_notify),
)

NAMES: tuple[str, ...] = tuple(p.name for p in PASSES)


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime | None = None,
    passes: Sequence[Pass] | None = None,
    run_id: int | None = None,
    record: bool = True,
) -> list[Outcome]:
    """Run the owed passes once, under the lock, and report what each one did.

    **The lock contract lives here, not in the callers.** Every writing pass runs inside
    a single `run_lock`, and a lock already held means another run is doing this work, so
    this one skips rather than blocking — the same conclusion `sync` reaches on
    `SyncLocked`. Before this module that contract was two-thirds absent: catchup took the
    lock and the four passes after it, replan included, did not.

    Rule 5 is the other half, and `record` is what completes it: a pass that raises is
    caught, written to `loop_pass` as `failed` with its exception, and the loop continues
    to the next one. One dead detector must not cost the notification the day needed —
    and must not be indistinguishable from a detector with nothing to find, which is what
    the old `except Exception: pass` made it.

    `record=False` is for a dry run, which reports without leaving a trace.
    """
    from backglass.plan import timezones
    from backglass.sync import SyncLocked, run_lock

    selected = tuple(passes if passes is not None else PASSES)
    if now is None:
        now = timezones.local_now(settings)

    try:
        with run_lock(settings):
            return [_one(p, conn, settings, now, run_id, record) for p in selected]
    except SyncLocked as locked:
        # Not silence: the whole loop skipping is a fact. A run that skipped because
        # another process was already doing this work and a run that had nothing to do
        # are different things, and only one of them means the loop is healthy.
        #
        # Recorded outside the lock, deliberately — these rows exist precisely because
        # the lock could not be taken, and a write that waited for it would deadlock the
        # explanation behind the thing it is explaining. `loop_pass` is append-only and
        # touched by nothing else, so it is not what the lock is protecting.
        at = _stamp(now)
        outcomes = [Outcome(p.name, SKIPPED, error=str(locked)) for p in selected]
        if record:
            for entry, outcome in zip(selected, outcomes, strict=True):
                _write(conn, entry, outcome, run_id, now, at, at)
        return outcomes


def _one(
    entry: Pass,
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    run_id: int | None,
    record: bool,
) -> Outcome:
    #: Wall clock, not the injected `now`: `started_at`/`finished_at` answer "how long did
    #: this take", and a frozen `now` would make every pass appear instantaneous. The
    #: injected clock decides *what the passes do*; it does not decide how long they took.
    started = now_iso()
    try:
        outcome = Outcome(entry.name, OK, tuple(entry.fn(conn, settings, now)))
    except Skip as skipped:
        outcome = Outcome(entry.name, SKIPPED, error=str(skipped))
    except Exception as exc:  # noqa: BLE001 — rule 5: degrade, never block
        outcome = Outcome(entry.name, FAILED, error=f"{type(exc).__name__}: {exc}")
    if record:
        _write(conn, entry, outcome, run_id, now, started, now_iso())
    return outcome


def _write(
    conn: sqlite3.Connection,
    entry: Pass,
    outcome: Outcome,
    run_id: int | None,
    now: datetime,
    started: str,
    finished: str,
) -> None:
    """One row, including for the quiet passes.

    The quiet ones are the point. A table holding only failures cannot answer "when did
    this last run at all", which is the question `state` and `heartbeat` ask — a pass that
    stopped being *called* leaves no failure to find, and that is exactly how the 05:45
    job went unnoticed for weeks (2026-08-17 lesson).

    Best-effort: a recording failure must not turn a healthy loop into a broken one. The
    row is the observation, not the work.
    """
    detail = outcome.error or ("\n".join(outcome.lines) or None)
    with contextlib.suppress(sqlite3.Error):
        conn.execute(
            "INSERT INTO loop_pass"
            " (user_id, run_id, name, trigger, status, local_date, started_at,"
            "  finished_at, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (USER_ID, run_id, entry.name, entry.trigger, outcome.status,
             now.date().isoformat(), started, finished, detail),
        )


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).replace(microsecond=0).isoformat()


# ── what the loop's own health looks like ─────────────────────────────────────


@dataclass(frozen=True)
class PassHealth:
    """One pass as a reader sees it: when it last succeeded, and what is wrong now.

    `last_ok` is None for a pass that has never succeeded — which on a fresh install is
    every one of them, and is why the readers below phrase their verdict against a grace
    window rather than against "has it ever run".
    """

    name: str
    last_ok: str | None
    last_status: str | None
    last_error: str | None
    consecutive_failures: int

    @property
    def failing(self) -> bool:
        return self.consecutive_failures > 0


def health(conn: sqlite3.Connection) -> list[PassHealth]:
    """Every declared pass, in registry order, with its last outcome.

    Reads the declared list rather than the distinct names in the table, so a pass that
    has never run once appears with `last_ok = None` instead of vanishing. A reader that
    enumerates only what the table holds cannot see the pass that was never called, and
    that is the failure this whole increment is about.
    """
    out: list[PassHealth] = []
    for entry in PASSES:
        rows = conn.execute(
            "SELECT status, finished_at, detail FROM loop_pass"
            " WHERE user_id = ? AND name = ? ORDER BY id DESC LIMIT 200",
            (USER_ID, entry.name),
        ).fetchall()
        last_ok = next((str(r["finished_at"]) for r in rows if r["status"] == OK), None)
        streak = 0
        for row in rows:
            # A skip is neither success nor failure — the pass was not attempted, so it
            # neither proves health nor breaks a failing streak. Counting it as a failure
            # would alarm on a contended lock, which is the normal case under launchd.
            if row["status"] == SKIPPED:
                continue
            if row["status"] != FAILED:
                break
            streak += 1
        out.append(
            PassHealth(
                name=entry.name,
                last_ok=last_ok,
                last_status=str(rows[0]["status"]) if rows else None,
                last_error=str(rows[0]["detail"]) if rows and rows[0]["detail"] else None,
                consecutive_failures=streak,
            )
        )
    return out


def failing(conn: sqlite3.Connection) -> list[PassHealth]:
    return [p for p in health(conn) if p.failing]


def recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM loop_pass WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (USER_ID, limit),
    ).fetchall()


def by_name(names: Sequence[str]) -> list[Pass]:
    """The passes these names refer to, in registry order.

    Raises on an unknown name rather than running the rest: `--only notifiy` silently
    doing nothing is worse than an error, because the owner reads "no output" as "nothing
    to do" and walks away believing the pass ran.
    """
    wanted = set(names)
    unknown = sorted(wanted - set(NAMES))
    if unknown:
        raise KeyError(f"unknown pass(es): {', '.join(unknown)}; known: {', '.join(NAMES)}")
    return [p for p in PASSES if p.name in wanted]


# ── who asks ──────────────────────────────────────────────────────────────────
# The registry above answers "what runs"; this answers "who starts it". Until goal 3 the
# two callers gave different answers to the second question and nobody could see it: the
# CLI ran five passes and opening the app ran one, because the app-open trigger was built
# inside `catchup` when catchup was the only thing it needed to fire.
#
# It is safe to widen because of what the passes already are, not because of care taken
# here. Every one of them is idempotent (rule 3) and gated on its own hour or its own
# ask-once key, which is exactly what the registry made checkable — `TestIdempotency`
# runs the whole loop twice and asserts the second writes nothing.

#: One loop at a time inside this process. The dashboard can be asked for the same page
#: by two tabs in the same second, and each would otherwise start its own planner. The
#: cross-process guard is `run_lock`; this is the in-process one, and both are needed.
_running = threading.Lock()


def on_open(settings: Settings, *, now: datetime | None = None) -> list[Outcome]:
    """The loop, for an app that has just been opened, on its own connection.

    Its own connection because the caller is a thread the request path started and the
    dashboard's connection belongs to a request. The lock is `run.`'s problem, not this
    function's — a held lock means another run is already doing the work and every pass
    comes back `skipped`.
    """
    from backglass.db import connect

    conn = connect(settings.db_path)
    try:
        outcomes = run(conn, settings, now=now)
        conn.commit()
    finally:
        conn.close()
    return outcomes


def spawn_on_open(settings: Settings) -> None:
    """Fire `on_open` on a daemon thread and return immediately.

    Fire and forget, for two reasons that are the same reason. The dashboard is what the
    desktop shell opens, so anything synchronous here is time the owner spends looking at
    a window that has not painted — and catching up a plan is model calls, seconds of
    them. And by rule 5 a failure to run the loop must degrade: the page still renders,
    the ledger is still correct, and `heartbeat` and `state` still say what is missing.

    Daemon, so quitting the app never waits on it; anything half-written is a proposal
    the next open regenerates, never a partial commit.
    """
    if not _running.acquire(blocking=False):
        return

    def work() -> None:
        try:
            for outcome in on_open(settings):
                for line in outcome.lines:
                    print(line, file=sys.stderr)
                if outcome.status == FAILED:
                    print(f"loop pass {outcome.name!r} failed: {outcome.error}",
                          file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — rule 5: never take the dashboard down
            print(f"loop on open failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            _running.release()

    threading.Thread(target=work, name="backglass-loop", daemon=True).start()
