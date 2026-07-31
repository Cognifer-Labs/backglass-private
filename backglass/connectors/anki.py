"""Anki. Phase A, docs/14 F2 — a local source in the shape of §Notes/iMessage.

The store is Anki's own `collection.anki2` (SQLite), opened plain ``mode=ro`` with a
short busy timeout — deliberately *not* ``immutable=1``, because the store is in WAL
mode and immutable makes SQLite skip the ``-wal`` file entirely: against a database
Anki has open that either misses every review committed only to the WAL (silently
stale tallies) or fails outright with "no such table". A read-only open takes only a
shared lock; if Anki holds the write lock past the timeout, the fetch fails and the
source degrades for one cycle (rule 5), which is the honest outcome. AnkiConnect was
rejected — it requires the Anki GUI running; a file read does not.

What is ingested is *exhaust*, not content: review tallies and due loads. No card
text ever leaves the store, which is why the docs/08 boundary has no surface here —
there are no addresses in a review count.

Two item shapes, both designed around the 0002 immutability trigger (a source_item
can never be updated, so nothing may be emitted whose content could later change):

  * ``reviews:<date>:<min-id>-<max-id>`` — one per local day per fetch batch: how
    many reviews landed that day in this batch, and the minutes they took. The
    external id names the exact revlog row range the tally was computed from, and
    revlog rows are append-only and immutable — so the same id can only ever carry
    the same content, and an immutability conflict is impossible by construction.
    A day that gains more reviews after a sync gets a *second* batch item (a new
    range) rather than a rewrite; the checkpoint wiring in `goals.reviews` treats
    later batches for an already-counted day as zero-delta markers, so target
    progress never double-counts. After a cursor loss, a full rescan emits one
    whole-day range per day — a new item alongside any old partial-batch items,
    which slightly overweights those days in the trailing pace estimate until they
    leave the window; accepted, because the alternative is a conflicted sync.
  * ``due:<date>:<count>`` — one snapshot per local day, taken by the first sync of
    the day (the 06:00 launchd run, in practice). Due counts drift all day, so the
    count is part of the id and `occurred_at` is pinned to the date's UTC midnight:
    every field is deterministic, so a rescan either re-serves the identical item
    (write-free) or emits a sibling with a different count — never a conflict. The
    reader takes the newest row for the date.

The revlog cursor is `revlog.id`, which is an epoch-millisecond primary key and
therefore monotonic — the same complete-watermark property the Messages ROWID has.
The cursor string is JSON: ``{"revlog": <max id>, "due_date": "YYYY-MM-DD"}``.
Anything unreadable means a full scan.

Anki scheduling facts the due count leans on: `col.crt` is the collection-creation
epoch (seconds, start of local day); for review and day-learn cards
(`queue` 2 and 3) `cards.due` is a day offset from that; for intraday learning
cards (`queue` 1) it is epoch seconds. Suspended and buried queues are negative
and excluded by `queue >= 0` plus the explicit queue filter.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash

_REVLOG = "SELECT id, time FROM revlog WHERE id > ? ORDER BY id"


@dataclass(kw_only=True)
class AnkiConnector:
    """Review tallies and due loads from a local Anki collection."""

    db_path: Path
    #: IANA zone the owner's days are counted in. Injected because the connector has
    #: no Settings; rule 4's spirit applies — a review at 23:30 in Phoenix belongs to
    #: the Phoenix day, not the UTC one.
    tz: str

    cursor: Cursor = None
    excluded: int = 0

    @property
    def name(self) -> str:
        return "anki"

    def health(self) -> Health:
        if not self.db_path.exists():
            return Health(
                name=self.name,
                ok=False,
                detail=f"Anki collection not found at {self.db_path}",
            )
        try:
            with closing(self._connect()) as conn:
                conn.execute("SELECT id FROM revlog LIMIT 1").fetchone()
                conn.execute("SELECT crt FROM col LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            return Health(
                name=self.name,
                ok=False,
                detail=f"cannot read {self.db_path}: {type(exc).__name__}: {exc}",
            )
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        watermark, due_date = _parse(since)
        today = self._today()

        with closing(self._connect()) as conn:
            highest = watermark
            days: dict[str, dict[str, int]] = {}
            for row in conn.execute(_REVLOG, (watermark,)):
                rid = int(row["id"])
                highest = max(highest, rid)
                day = self._local_day(rid / 1000)
                bucket = days.setdefault(
                    day, {"reviews": 0, "ms": 0, "min_id": rid, "max_id": 0}
                )
                bucket["reviews"] += 1
                bucket["ms"] += int(row["time"] or 0)
                bucket["min_id"] = min(bucket["min_id"], rid)
                bucket["max_id"] = max(bucket["max_id"], rid)

            for day in sorted(days):
                yield self._review_item(day, days[day])

            if due_date != today.isoformat():
                yield self._due_item(conn, today)

        self.cursor = json.dumps(
            {"revlog": highest, "due_date": today.isoformat()}, sort_keys=True
        )

    # ── item construction ────────────────────────────────────────────────

    def _review_item(self, day: str, bucket: dict[str, int]) -> SourceItem:
        minutes = round(bucket["ms"] / 60_000)
        occurred_at = (
            datetime.fromtimestamp(bucket["max_id"] / 1000, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
        )
        body = f"{bucket['reviews']} reviews · {minutes} min"
        return SourceItem(
            source=self.name,
            external_id=f"reviews:{day}:{bucket['min_id']}-{bucket['max_id']}",
            occurred_at=occurred_at,
            author=self.name,
            title=f"Anki reviews · {day}",
            body_text=body,
            raw_json=json.dumps(
                {"date": day, "reviews": bucket["reviews"], "minutes": minutes},
                sort_keys=True,
            ),
            content_hash=content_hash(
                author=self.name,
                title=f"Anki reviews · {day}",
                body_text=body,
                occurred_at=occurred_at,
            ),
        )

    def _due_item(self, conn: sqlite3.Connection, today: date) -> SourceItem:
        crt = int(conn.execute("SELECT crt FROM col LIMIT 1").fetchone()["crt"])
        day_offset = (today - datetime.fromtimestamp(crt, tz=UTC).date()).days
        end_of_day = int(
            datetime.combine(
                today + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo(self.tz)
            ).timestamp()
        )
        due = int(
            conn.execute(
                "SELECT "
                " (SELECT COUNT(*) FROM cards WHERE queue IN (2, 3) AND due <= ?) + "
                " (SELECT COUNT(*) FROM cards WHERE queue = 1 AND due <= ?) AS n",
                (day_offset, end_of_day),
            ).fetchone()["n"]
        )
        # Deterministic on purpose: the id carries the count and occurred_at is the
        # date's UTC midnight, so a rescan can never emit this id with new content.
        occurred_at = f"{today.isoformat()}T00:00:00+00:00"
        body = f"{due} cards due"
        return SourceItem(
            source=self.name,
            external_id=f"due:{today.isoformat()}:{due}",
            occurred_at=occurred_at,
            author=self.name,
            title=f"Anki due · {today.isoformat()}",
            body_text=body,
            raw_json=json.dumps({"date": today.isoformat(), "due": due}, sort_keys=True),
            content_hash=content_hash(
                author=self.name,
                title=f"Anki due · {today.isoformat()}",
                body_text=body,
                occurred_at=occurred_at,
            ),
        )

    # ── plumbing ─────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        # mode=ro, NOT immutable=1 — the store is WAL and immutable skips the -wal
        # file. See the module docstring.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    def _today(self) -> date:
        return datetime.now(tz=ZoneInfo(self.tz)).date()

    def _local_day(self, epoch_seconds: float) -> str:
        return (
            datetime.fromtimestamp(epoch_seconds, tz=UTC)
            .astimezone(ZoneInfo(self.tz))
            .date()
            .isoformat()
        )


def _parse(cursor: Cursor) -> tuple[int, str]:
    """``{"revlog": int, "due_date": str}`` → (watermark, snapshotted date).
    Anything unreadable is a full scan."""
    try:
        state = json.loads(str(cursor or ""))
        return int(state["revlog"]), str(state.get("due_date", ""))
    except (ValueError, TypeError, KeyError):
        return 0, ""
