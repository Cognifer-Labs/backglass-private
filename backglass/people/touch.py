"""Days-since-last-touch, mirroring goals/health.Staleness on purpose.

Same shape, same chip discipline (a day count, never a bare colour), separate type —
a person going quiet and a goal going quiet need different responses, which is the
same reason health.py refuses to merge staleness and risk.

Only curated profiles participate (role, org, or tags set) — the query enforces it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso, query
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class Touch:
    entity_id: int
    name: str
    role: str | None
    org: str | None
    days_since: int | None  # None when there has never been an interaction
    level: str  # new | fresh | warn | cold
    # Provenance of the most recent interaction; None only when days_since is None.
    source_row: dict[str, Any] | None
    #: The cadence this person is held to, in days, and whether they chose it. A chip
    #: that says "45 days since last touch" is a fact; whether that is late depends on
    #: this, and the two must be readable together or the number means nothing.
    every_days: int | None = None
    cadence_is_owners: bool = False

    def chip(self) -> str:
        if self.days_since is None:
            return "no interactions yet"
        return f"{self.days_since} days since last touch"

    def cadence_chip(self) -> str | None:
        """The owner's cadence, when they set one. Silent on the default, because a
        threshold nobody chose is not a fact about the relationship."""
        if not (self.cadence_is_owners and self.every_days):
            return None
        return f"every {self.every_days} days"


def cold(conn: sqlite3.Connection, settings: Settings, day: date) -> list[Touch]:
    """All curated profiles with their touch level, coldest first."""
    out: list[Touch] = []
    for row in conn.execute(query("people_cold"), {"user_id": USER_ID}):
        if row["last_touch_at"]:
            days: int | None = (day - date.fromisoformat(str(row["last_touch_at"])[:10])).days
        else:
            days = None
        # A per-person cadence, when the owner set one, replaces both thresholds: due at
        # the cadence, overdue at twice it. One number to choose rather than two, and
        # the doubling is what the global pair already does in spirit — a professor seen
        # each semester and a co-founder cannot share a clock, but neither needs the
        # owner to reason about a warn/cold split per person.
        every = row["touch_every_days"]
        cadence = int(every) if every else None
        warn_at = cadence or settings.people_touch_warn_days
        cold_at = cadence * 2 if cadence else settings.people_touch_cold_days
        if days is None:
            # Never interacted is young data, not a lapsed relationship: a class
            # roster imported yesterday must not render as a wall of alarm ink.
            # Format-audit ruling: vermilion means broken/overdue, nothing else.
            level = "new"
        elif days >= cold_at:
            level = "cold"
        elif days >= warn_at:
            level = "warn"
        else:
            level = "fresh"
        source_row = (
            {
                "source_item_id": row["source_item_id"],
                "source": row["source"],
                "source_external_id": row["source_external_id"],
                "source_occurred_at": row["source_occurred_at"],
                "source_title": row["source_title"],
            }
            if row["last_touch_at"]
            else None
        )
        out.append(
            Touch(
                entity_id=int(row["id"]),
                name=str(row["canonical_name"]),
                role=row["role"],
                org=row["org"],
                days_since=days,
                level=level,
                source_row=source_row,
                every_days=warn_at,
                cadence_is_owners=cadence is not None,
            )
        )
    return out


def needing_follow_up(
    conn: sqlite3.Connection, settings: Settings, day: date
) -> list[Touch]:
    """Warn-or-worse, with provenance — the brief's nudge source. Coldest first."""
    return [
        t
        for t in cold(conn, settings, day)
        if t.level in ("warn", "cold") and t.source_row is not None
    ]


class TouchError(ValueError):
    pass


#: What a recorded touch can be. Deliberately about what happened rather than how it
#: travelled: `met` is the one this exists for, and `sent` is separate from it because a
#: reach-out written and never answered is not the same relationship event as a
#: conversation, and the page should not print them the same way.
KINDS: dict[str, str] = {
    "met": "Met",
    "sent": "Sent a message",
    "call": "Spoke",
    "note": "Noted",
}


def record(
    conn: sqlite3.Connection,
    settings: Settings,
    entity_id: int,
    *,
    kind: str = "met",
    occurred_at: str | None = None,
    note: str | None = None,
) -> int:
    """Record a touch the ledger cannot see. Returns the touchpoint id.

    The owner's words become a manual `source_item` first and the touchpoint cites it —
    the shape `actions.quick_add` established, and the reason a hand-recorded dinner can
    appear in the brief at all: rule 1 asks for a source, not for a machine to have
    found it.

    Idempotent per day (migration 0023's unique index): the same touch logged twice is
    one touch, and re-running returns the existing row rather than halving the measured
    gap. The manual source item is written only once, for the same reason — a second
    identical claim in the evidence table is noise nobody asked for.
    """
    import uuid
    from hashlib import sha256

    from backglass.plan import timezones

    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise TouchError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    row = conn.execute(
        "SELECT canonical_name FROM entity WHERE id = ? AND user_id = ?",
        (entity_id, USER_ID),
    ).fetchone()
    if row is None:
        raise TouchError(f"no person with id {entity_id}")
    name = str(row["canonical_name"])

    when = (occurred_at or "").strip() or timezones.local_now_iso(settings)
    if occurred_at and occurred_at.strip():
        # Anything the owner typed is validated before it reaches the column. A
        # date-shaped string that is not a date is the failure quick_add already had
        # once — "tomorrow" stored as eight letters in a column the readers sort by —
        # and here it would silently anchor a relationship's clock to nothing.
        try:
            if len(when) == 10:
                # A bare day, and noon is the honest instant inside it (goals/reviews).
                when = timezones.local_noon_iso(settings, date.fromisoformat(when))
            else:
                datetime.fromisoformat(when)
        except ValueError as exc:
            raise TouchError(
                f"{when[:40]!r} is not a date; use YYYY-MM-DD"
            ) from exc

    existing = conn.execute(
        "SELECT id FROM touchpoint WHERE user_id = ? AND entity_id = ? AND kind = ?"
        " AND substr(occurred_at, 1, 10) = ?",
        (USER_ID, entity_id, kind, when[:10]),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    # The title is what the brief prints after "Last:", so it says what happened rather
    # than that something was typed. `quick_add`'s 'Manual entry' would render as
    # "Last: Manual entry", which is a sentence about the database.
    title = f"{KINDS[kind]}: {name}"
    body = (note or "").strip() or title
    cur = conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict, extraction_version)"
        " VALUES (?, 'manual', ?, ?, ?, ?, ?, ?, ?, 'keep', 'manual')",
        (
            USER_ID,
            uuid.uuid4().hex,
            now_iso(),
            when,
            (settings.owner_emails[0] if settings.owner_emails else settings.owner_name),
            title,
            body,
            sha256(f"touch:{entity_id}:{kind}:{when[:10]}:{body}".encode()).hexdigest(),
        ),
    )
    source_item_id = int(cur.lastrowid or 0)
    touch_id = conn.execute(
        "INSERT INTO touchpoint (user_id, entity_id, kind, occurred_at, note,"
        " source_item_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (USER_ID, entity_id, kind, when, (note or "").strip() or None,
         source_item_id, now_iso()),
    ).lastrowid
    return int(touch_id or 0)


def set_cadence(
    conn: sqlite3.Connection, entity_id: int, every_days: int | None
) -> None:
    """How often this person is worth a touch. None hands them back to the defaults."""
    if every_days is not None and every_days < 1:
        raise TouchError("a cadence is a number of days, at least 1")
    changed = conn.execute(
        "UPDATE entity SET touch_every_days = ? WHERE id = ? AND user_id = ?"
        " AND touch_every_days IS NOT ?",
        (every_days, entity_id, USER_ID, every_days),
    ).rowcount
    if not changed and conn.execute(
        "SELECT 1 FROM entity WHERE id = ? AND user_id = ?", (entity_id, USER_ID)
    ).fetchone() is None:
        raise TouchError(f"no person with id {entity_id}")


def history(conn: sqlite3.Connection, entity_id: int) -> list[dict[str, Any]]:
    """Recorded touches for one person, newest first, each with its source item."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT t.id, t.kind, t.occurred_at, t.note, t.source_item_id,"
            "       s.source, s.external_id AS source_external_id,"
            "       s.occurred_at AS source_occurred_at, s.title AS source_title"
            "  FROM touchpoint t JOIN source_item s ON s.id = t.source_item_id"
            " WHERE t.user_id = ? AND t.entity_id = ?"
            " ORDER BY datetime(t.occurred_at) DESC",
            (USER_ID, entity_id),
        )
    ]
