"""The operational commands, as things the dashboard can run.

The owner's directive (2026-08-24): the app should never need a terminal. The dashboard
already covered the *data* half — answering a question, accepting a fact, ticking a
target, merging two people — and none of the *operational* half. `sync`, `loop`, `logic`,
`backup`: every verb that makes the app run was a terminal command, which is why goal 3
kept finding surfaces the owner could not reach.

Three decisions hold this together, and each is the reason the obvious version is wrong.

**The registry holds callables, not command strings.** Every entry here already exists as
a function the CLI wraps — `sync.sync`, `loop.run`, `logic.run`, `backup.run`. The palette
calls the same function the terminal does, so there is no second implementation to drift,
no `uv run` path to get wrong on a machine where it moved, and no stdout to scrape. It
also means there is no path from the browser to a shell: the route takes a key from this
dict and can express nothing else.

**Destructive verbs are absent by decision, not by oversight.** `purge-boundary`, `prune`,
`restore` and `scrub` carry data-loss or legal weight (docs/08). A fuzzy search that puts
them one keystroke from a mis-click buys nothing the terminal does not already give, and
the owner ruled on this before it was built.

**A command reports what its own report says.** `sync` returning `degraded` prints
degraded. The palette has no vocabulary of its own for success, because a surface that
invents one is a surface that can be wrong in a way nothing else in this app is (rule 1).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backglass.config import Settings


#: A command's own account of what it did: the lines to show, and whether it went well.
#: `ok=False` is not an exception — `sync` finishing degraded is a successful run of a
#: command reporting bad news, and the two must not be rendered the same.
@dataclass(frozen=True)
class Result:
    lines: tuple[str, ...] = ()
    ok: bool = True


#: Every command takes the connection and the settings and nothing else. Commands that
#: genuinely need an argument are not in this registry yet — guessing a date for the
#: owner is worse than making them pick one, and increment 3 gives those a real input.
CommandFn = Callable[[sqlite3.Connection, Settings], Result]


@dataclass(frozen=True)
class Command:
    key: str
    label: str
    #: One sentence, shown under the name in the palette. What it does and what it costs,
    #: because "sync" and "backup" look equally cheap in a list and are not.
    blurb: str
    fn: CommandFn = field(repr=False)
    #: Whether it takes `run_lock` and can therefore be skipped by a running sync. Shown
    #: in the palette so "nothing happened" has a visible reason before it happens.
    locking: bool = False
    #: Whether it can reach the model backend, and so the spend cap.
    spends: bool = False


def _loop(conn: sqlite3.Connection, settings: Settings) -> Result:
    from backglass import loop as loop_mod

    lines: list[str] = []
    failed = False
    for outcome in loop_mod.run(conn, settings):
        lines.extend(outcome.lines)
        if outcome.status == loop_mod.FAILED:
            failed = True
            lines.append(f"{outcome.name} failed: {outcome.error}")
        elif outcome.status == loop_mod.SKIPPED and outcome.error:
            lines.append(f"{outcome.name} skipped: {outcome.error}")
    return Result(tuple(lines or ("nothing owed",)), ok=not failed)


def _logic(conn: sqlite3.Connection, settings: Settings) -> Result:
    from backglass import logic as logic_mod
    from backglass.plan import timezones

    report = logic_mod.run(conn, settings, timezones.local_now(settings).date())
    if not report.applied:
        return Result(("nothing the record contradicts",))
    by_rule = ", ".join(f"{rule} ×{n}" for rule, n in report.by_rule().items())
    return Result((f"disposed of {report.applied} item(s): {by_rule}",))


def _questions(conn: sqlite3.Connection, settings: Settings) -> Result:
    from backglass import questions as questions_mod
    from backglass.plan import timezones

    asked = questions_mod.refresh(conn, settings, timezones.local_now(settings).date())
    if not asked:
        return Result(("nothing new to ask",))
    return Result((f"{asked} new question(s) — answer at /ask",))


def _duplicates(conn: sqlite3.Connection, settings: Settings) -> Result:
    from backglass import duplicates as dup_mod
    from backglass import questions as questions_mod

    found = dup_mod.clusters(conn)
    asked = questions_mod.record(conn, dup_mod.questions_for(conn))
    return Result((
        f"{len(found)} cluster(s) over the open set",
        f"{asked} raised as cards — answer at /ask" if asked else "nothing new to card",
    ))


def _noise(conn: sqlite3.Connection, settings: Settings) -> Result:
    from backglass import questions as questions_mod
    from backglass.extract import noise as noise_mod

    asked = questions_mod.record(conn, noise_mod.questions_for(conn, settings))
    if not asked:
        return Result(("no senders waiting",))
    return Result((f"{asked} sender(s) to decide about — /ask",))


def _notifications(conn: sqlite3.Connection, settings: Settings) -> Result:
    from backglass import notify as notify_mod

    sent = notify_mod.run(conn, settings)
    if not sent:
        return Result(("nothing owed, or outside your notify window",))
    return Result(tuple(f"{n.title} ({n.delivered})" for n in sent))


def _backup(conn: sqlite3.Connection, settings: Settings) -> Result:
    """Snapshot then rotate, in that order — the same two calls the CLI makes.

    Rotation after the snapshot, never before: rotating first would delete the oldest copy
    to make room for one that has not been written yet, and a snapshot that then fails
    leaves the owner with fewer backups than they started with.
    """
    from backglass import backup as backup_mod

    del conn
    path = backup_mod.snapshot(settings.db_path, settings.backup_dir)
    removed = backup_mod.rotate(settings.backup_dir)
    lines = [f"wrote {path.name}"]
    if removed:
        lines.append(f"rotated out {len(removed)} older snapshot(s)")
    return Result(tuple(lines))


def _state(conn: sqlite3.Connection, settings: Settings) -> Result:
    """The verdicts, not the forty fields. A palette is a place to learn something is
    wrong, not to read a full report — `/state` in the terminal stays the long form."""
    from backglass import state as state_mod

    checks = state_mod.verdicts(state_mod.collect(conn, settings), conn, settings)
    bad = [v for v in checks if not v.ok]
    if not bad:
        return Result((f"all {len(checks)} checks pass",))
    return Result(tuple(v.line() for v in bad), ok=False)


def _index(conn: sqlite3.Connection, settings: Settings) -> Result:
    from backglass import search

    added = search.index(conn, settings)
    return Result((f"{added} document(s) indexed",) if added else ("nothing left to index",))


#: What the palette offers. Deliberately short, and short for a reason the owner ruled on:
#: the destructive verbs (`purge-boundary`, `prune`, `restore`, `scrub`) are not here and
#: are not meant to be. Adding one is a decision about data loss, not about convenience.
COMMANDS: tuple[Command, ...] = (
    Command("loop", "Run the loop", "Catch up, dispose, detect, plan, notify — the whole"
            " recurring pass set, once.", _loop, locking=True, spends=True),
    Command("logic", "Check the logic", "Throw out what the record already contradicts."
            " Deterministic; costs nothing.", _logic),
    Command("questions", "Look for questions", "Run the detectors over the current board.",
            _questions),
    Command("duplicates", "Find duplicates", "Cluster open commitments that look like one"
            " promise and raise them as cards. Takes a few seconds.", _duplicates),
    Command("noise", "Review noisy senders", "Senders that have cost a lot and produced"
            " nothing, as cards you can answer.", _noise),
    Command("notifications", "Send what's owed", "Deliver today's notifications, if the"
            " window is open.", _notifications),
    Command("index", "Index documents", "Embed whatever retrieval cannot reach yet."
            " Local model; costs nothing.", _index),
    Command("state", "Check this installation", "Every verdict that is currently failing,"
            " and what would clear it.", _state),
    Command("backup", "Back up the ledger", "A snapshot of the database, kept on disk.",
            _backup),
)

BY_KEY: dict[str, Command] = {c.key: c for c in COMMANDS}


def get(key: str) -> Command | None:
    """The command this key names, or None. The route's only lookup — a key that is not
    in the registry can express nothing, which is what keeps the browser away from a
    shell."""
    return BY_KEY.get(key)


def as_rows() -> list[dict[str, Any]]:
    return [
        {"key": c.key, "label": c.label, "blurb": c.blurb,
         "locking": c.locking, "spends": c.spends}
        for c in COMMANDS
    ]
