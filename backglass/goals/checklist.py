"""The daily checklist. docs/04 §2.6.

    "Separate from goals, and deliberately so. The checklist is the small set of daily
    non-negotiables — the things that are habits rather than projects."

  C1  Maximum seven items. "The cap is enforced, not advisory."
  C2  Items are binary. No partial credit, no percentages.
  C3  Items may be weekday-scoped.
  C4  Streaks are tracked and shown, but a broken streak resets quietly. "No commiserating
      copy, no flame icons, no 'you lost your 40-day streak.'"
  C5  The checklist appears in the brief only if incomplete items remain by the evening
      pass, and in the dashboard always.
  C6  Checklist state is per local day in the active timezone, and a timezone change does
      not retroactively break a streak.

C4 and C6 are the two that are easy to get wrong in opposite directions. C4 is about tone —
`streak()` returns an integer and there is deliberately no function here that renders a
sentence about a broken one. C6 is about correctness: a streak is counted over the days
that were *scheduled*, so flying east and skipping a Saturday that was never a checklist
day cannot break anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan import timezones

CAP = 7
#: specs/schema.sql: `weekday_mask INTEGER NOT NULL DEFAULT 127, -- bit 0 = Monday`
ALL_DAYS = 127


class ChecklistError(ValueError):
    pass


def weekday_bit(day: date) -> int:
    return 1 << day.weekday()


def scheduled_on(mask: int, day: date) -> bool:
    """C3. Weekday-scoped items."""
    return bool(mask & weekday_bit(day))


@dataclass
class Item:
    id: int
    title: str
    weekday_mask: int
    ticked: bool
    streak: int


def add(conn: sqlite3.Connection, title: str, *, weekday_mask: int = ALL_DAYS) -> int:
    """C1. The cap is enforced here, with an error that says why.

    docs/04: "A checklist of twenty is a list nobody completes and the empty boxes become
    invisible." The message repeats the reason because a bare "limit reached" invites
    someone to raise the limit.
    """
    count = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM checklist_item WHERE user_id = ? AND active = 1",
            (USER_ID,),
        ).fetchone()["n"]
    )
    if count >= CAP:
        raise ChecklistError(
            f"the checklist is capped at {CAP} items and has {count}. A list of "
            "non-negotiables that runs to eight is a list nobody completes, and the empty "
            "boxes become invisible. Deactivate one first."
        )
    conn.execute(
        "INSERT INTO checklist_item (user_id, title, weekday_mask, sort_order) "
        "VALUES (?, ?, ?, ?)",
        (USER_ID, title, weekday_mask, count),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def tick(conn: sqlite3.Connection, item_id: int, day: date) -> None:
    """C2. Binary. The row exists or it does not; there is no percentage column."""
    conn.execute(
        "INSERT INTO checklist_tick (checklist_item_id, local_date, ticked_at) "
        "VALUES (?, ?, ?) ON CONFLICT (checklist_item_id, local_date) DO NOTHING",
        (item_id, day.isoformat(), now_iso()),
    )


def untick(conn: sqlite3.Connection, item_id: int, day: date) -> None:
    conn.execute(
        "DELETE FROM checklist_tick WHERE checklist_item_id = ? AND local_date = ?",
        (item_id, day.isoformat()),
    )


def streak(conn: sqlite3.Connection, settings: Settings, item_id: int, day: date) -> int:
    """C4 and C6. Consecutive *scheduled* days ticked, ending today or yesterday.

    Only days the item was scheduled on count, so a weekday-only habit is not broken by
    Sunday. Today counts if ticked but does not break the streak if not — the day is not
    over yet, and a counter that drops to zero at 00:01 and back at 18:00 is noise.

    Returns a number. There is deliberately no companion function that renders a sentence
    about losing one.
    """
    row = conn.execute(
        "SELECT weekday_mask FROM checklist_item WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return 0
    mask = int(row["weekday_mask"])
    ticked = {
        str(r["local_date"])
        for r in conn.execute(
            "SELECT local_date FROM checklist_tick WHERE checklist_item_id = ?", (item_id,)
        )
    }

    count = 0
    cursor = day
    if not (scheduled_on(mask, cursor) and cursor.isoformat() in ticked):
        cursor -= timedelta(days=1)  # today is not over; start from yesterday

    guard = 0
    while guard < 3650:
        guard += 1
        if not scheduled_on(mask, cursor):
            cursor -= timedelta(days=1)
            continue
        if cursor.isoformat() not in ticked:
            break
        count += 1
        cursor -= timedelta(days=1)
    return count


def today(conn: sqlite3.Connection, settings: Settings, day: date) -> list[Item]:
    """C3 and C6. What is due today, in the day's active timezone."""
    del settings  # the caller resolved `day` in the active zone already
    rows = conn.execute(
        "SELECT i.id, i.title, i.weekday_mask, t.id AS tick_id "
        "FROM checklist_item i "
        "LEFT JOIN checklist_tick t ON t.checklist_item_id = i.id AND t.local_date = ? "
        "WHERE i.user_id = ? AND i.active = 1 AND (i.weekday_mask & ?) != 0 "
        "ORDER BY i.sort_order, i.id",
        (day.isoformat(), USER_ID, weekday_bit(day)),
    ).fetchall()
    return [
        Item(
            id=int(r["id"]),
            title=str(r["title"]),
            weekday_mask=int(r["weekday_mask"]),
            ticked=r["tick_id"] is not None,
            streak=0,
        )
        for r in rows
    ]


def with_streaks(conn: sqlite3.Connection, settings: Settings, day: date) -> list[Item]:
    items = today(conn, settings, day)
    for item in items:
        item.streak = streak(conn, settings, item.id, day)
    return items


def incomplete(conn: sqlite3.Connection, settings: Settings, day: date) -> list[Item]:
    """C5. The brief shows the checklist only when something is still open."""
    return [item for item in today(conn, settings, day) if not item.ticked]


def local_day(settings: Settings, moment: date | None = None) -> date:
    """C6. The local day in the active timezone.

    A tick made at 22:00 in Coimbatore and one made at 09:30 in Phoenix belong to
    different local days even when they are hours apart in UTC, and the streak is counted
    over local days.
    """
    if moment is not None:
        return moment
    from datetime import datetime
    from zoneinfo import ZoneInfo

    provisional = datetime.now(ZoneInfo(settings.default_tz)).date()
    return datetime.now(ZoneInfo(timezones.active_tz(settings, provisional))).date()
