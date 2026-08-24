"""Items the upstream store no longer has.

A windowed connector — one with no modification watermark, that re-reads a fixed span on
every run — knows something no incremental connector does: it has just seen *everything*
the store holds for that span. Anything the ledger has in the same span that the read did
not return is gone from upstream. That is positive evidence, not silence, and it is the
only kind this module acts on.

It exists because the owner's class schedule changed on ~2026-08-10 and Backglass did not
notice. Calendar.app dropped LIA 101, moved a BIO 181 lecture off Thursday, and removed a
BIO lab, a CHM lab and a CHM recitation. The connector re-read its window, collected the
new timetable, and wrote it alongside the old one — because `source_item` is immutable and
nothing in the pipeline could say "that event is no longer real". The planner went on
subtracting five dead class slots from the owner's day, and every health check stayed
green, because nothing was broken. There were simply two schedules and no way to tell
which one the world agreed with.

**The safety property, which matters more than the feature.** A retraction is only ever
inferred from a read the connector itself certifies as complete, and certification is per
sub-store. `apple_calendar` issues one Apple Event per calendar and rule 5 says a calendar
that times out costs its own events and nothing else — so a partial read is normal,
expected, and looks exactly like "half the owner's classes were cancelled". A calendar
that failed is therefore not in scope, and its rows are untouchable until a run reads it
through; a calendar that succeeded is evidence about itself and nothing else.

That scoping is not a refinement, it is what makes the feature usable. The first version
refused unless *every* calendar succeeded, and the owner has one that takes minutes and
intermittently fails — so it vetoed retraction for every other calendar on every run, and
the cancelled class kept its slot regardless.

Two near-misses are worth stating, because both passed the whole test suite. A JXA
`catch (e) { continue; }` turned macOS's -1712 Apple Event timeout into an empty calendar
with exit code 0, so a timed-out read was indistinguishable from an emptied one. And
`seen_ids` recorded what the connector *emitted* rather than what the store *returned*,
so a duplicate the connector deliberately suppresses — the owner's HON 171, PSY 101 and
CIS 236 each sit in two calendars — read as deleted. Together they put this one clean run
away from retracting sixteen live classes. It was caught by printing the would-delete list
and reading it, not by a test.

Retractions are additive and reversible: the `source_item` is untouched, so an item
wrongly retracted is restored by deleting one row, and an item retracted correctly is
still readable by anyone asking what last August looked like.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from backglass.db import now_iso
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class RetractableWindow:
    """One certified read: what span, what was in it, and which sub-stores it covers.

    `calendars` is the scope, and it exists because certification has to be finer than a
    whole connector. The owner has one calendar that takes minutes and intermittently
    fails; under an all-or-nothing rule that calendar vetoed retraction for every other
    one on every run, and a cancelled class kept its slot in the day plan indefinitely.

    A connector with no meaningful sub-stores passes `calendars=None`, which means "this
    read covers everything this source has" and skips the scope filter entirely.
    """

    starts_at: str
    ends_before: str
    seen_ids: set[str]
    #: `raw_json` keys whose rows this read is evidence about. None = all of them.
    calendars: set[str] | None = None
    #: Where in `raw_json` the sub-store's name lives.
    scope_field: str = "calendar"


@runtime_checkable
class Reconcilable(Protocol):
    """A connector that can certify a complete read of a bounded window.

    Optional, like `base.Countable`: `reconcile` uses `getattr` and a connector that does
    not implement this is left alone. Incremental connectors must *not* implement it —
    a cursor means the read was never asked for everything, so absence proves nothing.
    """

    @property
    def name(self) -> str: ...

    def retractable_window(self) -> RetractableWindow | None:
        """The certified read, or None.

        None means "do not infer anything from this read" and is the correct answer
        whenever the read was partial, skipped, or never happened.
        """
        ...


def reconcile(conn: sqlite3.Connection, connector: Any) -> list[int]:
    """Retract this connector's ledger rows that its latest complete read did not return.

    Returns the `source_item` ids newly retracted, so a caller can report a number the
    owner can act on — "5 calendar events are no longer on your calendar" is a sentence
    worth putting in a sync line; a silent tidy-up is not.

    Bounded by the window on both ends. Rows outside it were never in scope for this read
    and say nothing about the store's current contents — retracting them on absence would
    delete the owner's history the moment a connector narrowed its horizon.
    """
    window = getattr(connector, "retractable_window", None)
    if window is None:
        return []
    result = window()
    if result is None:
        return []

    rows = conn.execute(
        "SELECT si.id, si.external_id, si.raw_json FROM source_item si "
        "WHERE si.user_id = ? AND si.source = ? "
        "  AND datetime(si.occurred_at) >= datetime(?) "
        "  AND datetime(si.occurred_at) < datetime(?) "
        "  AND NOT EXISTS (SELECT 1 FROM source_item_retraction r "
        "                  WHERE r.source_item_id = si.id)",
        (USER_ID, connector.name, result.starts_at, result.ends_before),
    ).fetchall()

    gone = [
        int(row["id"])
        for row in rows
        if _in_scope(row, result) and str(row["external_id"]) not in result.seen_ids
    ]
    if not gone:
        return []

    reason = (
        f"{connector.name} re-read {result.starts_at} .. {result.ends_before} in full "
        "and no longer returns this item"
    )
    stamp = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO source_item_retraction "
        "(source_item_id, user_id, retracted_at, reason) VALUES (?, ?, ?, ?)",
        [(item_id, USER_ID, stamp, reason) for item_id in gone],
    )
    return gone


def _in_scope(row: Any, window: RetractableWindow) -> bool:
    """Is this row one the read is evidence about?

    A row whose sub-store was not read completely is not evidence of anything, and the
    conservative answer is the only safe one: a row we cannot place stays. Unparseable
    `raw_json`, a missing key, a calendar that failed — all of them mean "leave it",
    because the cost of a wrong retraction is an event silently vanishing from the
    owner's day and the cost of a missed one is a stale row that the next clean read
    picks up anyway.
    """
    if window.calendars is None:
        return True
    try:
        payload = json.loads(str(row["raw_json"] or "{}"))
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    name = payload.get(window.scope_field)
    return isinstance(name, str) and name in window.calendars


def retracted_ids(conn: sqlite3.Connection) -> set[int]:
    """Every retracted item, for a caller that already holds its rows in memory."""
    return {
        int(row["source_item_id"])
        for row in conn.execute(
            "SELECT source_item_id FROM source_item_retraction WHERE user_id = ?",
            (USER_ID,),
        )
    }


def restore(conn: sqlite3.Connection, source_item_id: int) -> bool:
    """Undo one retraction. Returns whether there was one to undo.

    The reversal half of the safety property. A retraction is an inference, and an
    inference the owner disagrees with has to be answerable by something other than
    editing the database by hand.
    """
    cursor = conn.execute(
        "DELETE FROM source_item_retraction WHERE source_item_id = ? AND user_id = ?",
        (source_item_id, USER_ID),
    )
    return bool(cursor.rowcount)
