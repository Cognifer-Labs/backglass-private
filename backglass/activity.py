"""What the ledger changed, what an upstream source withdrew, and what the system said.

Three tables were written between 0030 and 0032 and none of them ever reached a surface.
On 2026-08-25 they held 421 claim events, 18 retractions and 28 notifications, and
`grep -rn` over `backglass/web/` found no reader for any of them. That is the gap this
module closes: CLAUDE.md rule 1 asks every claim to link to its source, and an audit trail
the owner cannot open is a promise the schema keeps and the product does not.

**A reader, like `courses.py`.** No table, no migration, nothing stored. It reads
`claim_event`, `source_item_retraction` and `notification`, orders them by time, and hands
back rows. If this module vanished every other surface would still be correct.

Three decisions worth stating, because each one was a choice:

1. **A change where the old value equals the new value is not a change.** 345 of the 421
   claim events on the owner's ledger are `relevance_rejudged` rows recording `keep → keep`
   — the re-judgement ran and agreed with itself. Listing them would bury the 76 rows that
   actually moved under a 94% majority that did not. They are counted and named on the
   page rather than dropped, because "nothing changed" is a real thing to have learned and
   silently discarding it would misreport how much work the pipeline did.

2. **A retraction's reason is a sentence about a window, not about the item.** Every row
   reads `"<source> re-read <from> .. <to> in full and no longer returns this item"`. It is
   the right thing to store — it is the evidence — and the wrong thing to show eighteen
   times, so the feed states the source and the fact of withdrawal and keeps the full
   sentence for the detail line.

3. **Time is the only ordering.** Not stream, not severity. The question the page answers
   is "what happened, most recent first"; a feed grouped by kind is three lists, and three
   lists are what the owner already had in three tables nobody could read.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from backglass.ledger import USER_ID

#: The streams a reader may ask for. Also the filter vocabulary the page uses, so a bad
#: query string is caught here rather than becoming an empty page with no explanation.
STREAMS = ("change", "retraction", "notice")

#: How far back the page looks by default. Long enough that a quiet week still has
#: something on it, short enough that the first paint is not the whole history.
DEFAULT_DAYS = 30

#: Rows per page. A feed with no ceiling is a feed that renders the whole ledger the day
#: someone re-runs extraction.
DEFAULT_LIMIT = 200


@dataclass(frozen=True)
class Event:
    """One thing that happened, from whichever of the three tables reported it."""

    at: str
    stream: str
    #: The headline: what the event is about, in the owner's vocabulary.
    title: str
    #: The evidence line under it — a quote, a reason, a notification body.
    detail: str
    #: Why it happened. `claim_event.cause`, the notification kind, or the source name.
    cause: str
    old: str | None = None
    new: str | None = None
    #: Where to go to see the subject, when the subject has a page. `None` is honest:
    #: a commitment has no detail page and linking to the board would be a guess.
    href: str | None = None

    @property
    def day(self) -> str:
        """The UTC date, for grouping. The page groups by day and says which zone."""
        return self.at[:10]


@dataclass(frozen=True)
class Feed:
    events: list[Event] = field(default_factory=list)
    #: Claim events inside the window whose old value equalled their new value.
    unchanged: int = 0
    #: Per-stream totals inside the window, before the limit — so the page can say what
    #: it did not draw instead of implying the feed is everything.
    totals: dict[str, int] = field(default_factory=dict)
    #: True when `limit` cut the list short.
    truncated: bool = False
    days: int = DEFAULT_DAYS
    stream: str | None = None


def load(
    conn: sqlite3.Connection,
    *,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    stream: str | None = None,
    now: datetime | None = None,
) -> Feed:
    """The three streams, merged and ordered newest first.

    `stream` narrows to one of `STREAMS`; anything else is treated as no filter, because
    a typo in a query string should show the feed, not an empty page.
    """
    if stream not in STREAMS:
        stream = None
    moment = now or datetime.now(UTC)
    since = (moment - timedelta(days=days)).isoformat()

    events: list[Event] = []
    totals: dict[str, int] = {}
    unchanged = 0

    if stream in (None, "change"):
        rows, unchanged = _changes(conn, since)
        totals["change"] = len(rows)
        events.extend(rows)
    if stream in (None, "retraction"):
        rows = _retractions(conn, since)
        totals["retraction"] = len(rows)
        events.extend(rows)
    if stream in (None, "notice"):
        rows = _notices(conn, since)
        totals["notice"] = len(rows)
        events.extend(rows)

    events.sort(key=lambda e: e.at, reverse=True)
    return Feed(
        events=events[:limit],
        unchanged=unchanged,
        totals=totals,
        truncated=len(events) > limit,
        days=days,
        stream=stream,
    )


# ── the three streams ─────────────────────────────────────────────────────────


def _changes(conn: sqlite3.Connection, since: str) -> tuple[list[Event], int]:
    """`claim_event`, minus the re-judgements that re-judged nothing.

    The subject's own words are joined in where the subject has words: a commitment says
    what it is, a fact says which lane and key it belongs to. Without that the feed reads
    "commitment 329 changed", which is a row id and not a sentence.
    """
    rows = conn.execute(
        """
        SELECT e.at, e.subject_table, e.subject_id, e.field, e.old_value, e.new_value,
               e.cause, c.what AS commitment_what, f.subject AS fact_subject,
               f.key AS fact_key
          FROM claim_event e
          LEFT JOIN commitment c
                 ON e.subject_table = 'commitment' AND c.id = e.subject_id
          LEFT JOIN fact f
                 ON e.subject_table = 'fact' AND f.id = e.subject_id
         WHERE e.user_id = ? AND e.at >= ?
         ORDER BY e.id DESC
        """,
        (USER_ID, since),
    ).fetchall()

    out: list[Event] = []
    unchanged = 0
    for r in rows:
        old, new = r["old_value"], r["new_value"]
        if old is not None and new is not None and old == new:
            unchanged += 1
            continue
        out.append(
            Event(
                at=r["at"],
                stream="change",
                title=_subject_title(r),
                detail=_change_detail(r["field"], old, new),
                cause=r["cause"],
                old=old,
                new=new,
                href=_subject_href(r["subject_table"]),
            )
        )
    return out, unchanged


def _retractions(conn: sqlite3.Connection, since: str) -> list[Event]:
    """Rows an upstream source stopped returning. 0030's whole point is that this used
    to be silent — the calendar dropped LIA 101 and moved a BIO 181 lecture off Thursday
    and nothing anywhere said so."""
    rows = conn.execute(
        """
        SELECT r.retracted_at, r.reason, r.source_item_id, si.source, si.title
          FROM source_item_retraction r
          JOIN source_item si ON si.id = r.source_item_id
         WHERE r.user_id = ? AND r.retracted_at >= ?
         ORDER BY r.retracted_at DESC
        """,
        (USER_ID, since),
    ).fetchall()
    return [
        Event(
            at=r["retracted_at"],
            stream="retraction",
            title=r["title"] or f"item {r['source_item_id']}",
            # The stored reason names the window it re-read, which is the evidence and
            # is also the same sentence every time. Rendered raw it is a wall of
            # microsecond timestamps — "re-read 2026-08-18T20:02:28.745075+00:00 ..
            # 2026-09-15T20:02:28.745075+00:00" — that pushes the item's own name out of
            # sight. The claim is unchanged and the row links to the item; only the
            # instants are read at the precision a person reads them.
            detail=_readable_window(r["reason"]),
            cause=r["source"],
            href=f"/source/{r['source_item_id']}",
        )
        for r in rows
    ]


def _notices(conn: sqlite3.Connection, since: str) -> list[Event]:
    """What the system said out loud, and whether saying it worked.

    `delivered` is either `osascript` or `failed: <why>`; the ledger answers "what did the
    system tell the owner and when", which Notification Center's memory cannot.
    """
    rows = conn.execute(
        """
        SELECT created_at, kind, title, body, delivered
          FROM notification
         WHERE user_id = ? AND created_at >= ?
         ORDER BY id DESC
        """,
        (USER_ID, since),
    ).fetchall()
    return [
        Event(
            at=r["created_at"],
            stream="notice",
            title=r["title"],
            detail=r["body"],
            cause=r["kind"],
            # Not a link: `new` carries the delivery outcome so a banner that never
            # reached the screen is visible as such rather than looking identical to
            # one that did.
            new=r["delivered"],
        )
        for r in rows
    ]


# ── wording ───────────────────────────────────────────────────────────────────


def _subject_title(row: sqlite3.Row) -> str:
    if row["commitment_what"]:
        return str(row["commitment_what"])
    if row["fact_subject"]:
        return f"{row['fact_subject']}/{row['fact_key']}"
    return f"{row['subject_table']} {row['subject_id']}"


def _change_detail(field_name: str | None, old: str | None, new: str | None) -> str:
    """The change as a sentence. A whole-row event has no field and says so."""
    what = field_name or "the row"
    if old is None and new is not None:
        return f"{what} set to {new}"
    if new is None and old is not None:
        return f"{what} cleared (was {old})"
    if old is None and new is None:
        return what
    return f"{what}: {old} → {new}"


#: An ISO instant inside a retraction reason. Date and hour are what a person needs to
#: place the re-read; the microseconds are what make the sentence unreadable.
_INSTANT = re.compile(r"(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def _readable_window(reason: str) -> str:
    """The stored reason with its instants shortened to dates.

    Presentation only, and deliberately lossy in one direction that cannot change the
    claim: a window stated to the microsecond and a window stated to the day assert the
    same withdrawal. The untrimmed text is still in `source_item_retraction.reason`, and
    the row links to the item it is about.
    """
    return _INSTANT.sub(r"\1", reason)


def _subject_href(subject_table: str) -> str | None:
    """Where the subject lives, when it lives anywhere.

    A commitment has no page of its own — the board is a list, not an address — so it
    gets no link rather than a link to somewhere the owner then has to search.
    """
    return {"fact": "/memory"}.get(subject_table)
