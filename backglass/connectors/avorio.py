"""Avorio — a flashcard app with a local SQLite store, read the same way as Anki.

The store is `~/Library/Application Support/Avorio/avorio.db` (path injected, per the
house rule), a Rust-core SQLite schema. Same contract as `anki.py`: opened plain
``mode=ro`` with a busy timeout (NOT ``immutable=1`` — the store is WAL, and
immutable skips the ``-wal`` file; see anki.py's docstring for the failure mode),
only review tallies and due loads ingested, never card content, so the docs/08
boundary has no surface. Item shapes and their conflict-impossible-by-construction
reasoning are documented in `anki.py` and identical here, with the row range in a
tally's external id expressed as ``<min-reviewed_at>-<max-reviewed_at>``.

Schema facts this connector depends on (Avorio migrations V1–V21):

  * ``reviews.reviewed_at`` — TEXT, ISO-8601, written by SQLite ``datetime('now')``,
    i.e. **UTC** with second precision. There is no monotonic integer id (the PK is a
    UUID string), so the watermark is ``MAX(reviewed_at)`` at full stored precision,
    compared with ``>``. Two reviews landing in the same second around a sync leave a
    sub-second blind spot; accepted and recorded here — a lost tally in a day count
    is one card, and the alternative (``>=`` plus re-served rows) would change an
    already-emitted day item's content, which 0002 forbids.
  * ``cards.due_date`` — TEXT ``YYYY-MM-DD``; ``card_state`` in
    ``new|learning|review|relearning``; ``suspended``/``buried`` integer flags.
  * ``reviews.duration_ms`` — integer milliseconds per review.

Avorio's schema is still moving (the Android port is in flight), so `health()` is a
schema guard, not just a file check: every required table and column is verified at
open, and a mismatch degrades the source with a detail naming exactly what moved
(rule 5) instead of crashing the sync. The stable fix is a versioned export view
inside Avorio; until then this guard is the tripwire.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash

#: table → columns this connector reads. Checked by health(); a miss names itself.
_REQUIRED = {
    "reviews": ("reviewed_at", "duration_ms"),
    "cards": ("due_date", "card_state", "suspended", "buried"),
}

_REVIEWS = (
    "SELECT reviewed_at, duration_ms FROM reviews "
    "WHERE reviewed_at > ? ORDER BY reviewed_at"
)

_DUE = (
    "SELECT COUNT(*) AS n FROM cards "
    "WHERE suspended = 0 AND buried = 0 "
    "AND card_state IN ('learning', 'review', 'relearning') AND due_date <= ?"
)


@dataclass
class _DayBucket:
    reviews: int = 0
    ms: int = 0
    min_at: str = ""
    max_at: str = ""


@dataclass(kw_only=True)
class AvorioConnector:
    """Review tallies and due loads from the owner's Avorio store."""

    db_path: Path
    #: IANA zone the owner's days are counted in — see anki.py on why this is injected.
    tz: str

    cursor: Cursor = None
    excluded: int = 0

    @property
    def name(self) -> str:
        return "avorio"

    def health(self) -> Health:
        if not self.db_path.exists():
            return Health(
                name=self.name,
                ok=False,
                detail=f"Avorio store not found at {self.db_path}",
            )
        try:
            with closing(self._connect()) as conn:
                missing = _schema_gaps(conn)
        except sqlite3.Error as exc:
            return Health(
                name=self.name,
                ok=False,
                detail=f"cannot read {self.db_path}: {type(exc).__name__}: {exc}",
            )
        if missing:
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    "Avorio schema moved under the connector: missing "
                    + ", ".join(missing)
                    + " — the stable fix is a versioned export view inside Avorio"
                ),
            )
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        watermark, due_date = _parse(since)
        today = self._today()

        with closing(self._connect()) as conn:
            if _schema_gaps(conn):
                # health() reports the detail; fetch refuses to guess at a moved schema.
                raise RuntimeError("Avorio schema mismatch — see health()")

            highest = watermark
            days: dict[str, _DayBucket] = {}
            for row in conn.execute(_REVIEWS, (watermark,)):
                at = str(row["reviewed_at"])
                highest = max(highest, at)
                day = self._local_day(at)
                bucket = days.setdefault(day, _DayBucket(min_at=at))
                bucket.reviews += 1
                bucket.ms += int(row["duration_ms"] or 0)
                bucket.min_at = min(bucket.min_at, at)
                bucket.max_at = max(bucket.max_at, at)

            for day in sorted(days):
                yield self._review_item(day, days[day])

            if due_date != today.isoformat():
                yield self._due_item(conn, today)

        self.cursor = json.dumps(
            {"reviewed_at": highest, "due_date": today.isoformat()}, sort_keys=True
        )

    # ── item construction ────────────────────────────────────────────────

    def _review_item(self, day: str, bucket: _DayBucket) -> SourceItem:
        reviews = bucket.reviews
        minutes = round(bucket.ms / 60_000)
        occurred_at = _utc_iso(bucket.max_at)
        body = f"{reviews} reviews · {minutes} min"
        title = f"Avorio reviews · {day}"
        return SourceItem(
            source=self.name,
            external_id=f"reviews:{day}:{bucket.min_at}-{bucket.max_at}",
            occurred_at=occurred_at,
            author=self.name,
            title=title,
            body_text=body,
            raw_json=json.dumps(
                {"date": day, "reviews": reviews, "minutes": minutes}, sort_keys=True
            ),
            content_hash=content_hash(
                author=self.name, title=title, body_text=body, occurred_at=occurred_at
            ),
        )

    def _due_item(self, conn: sqlite3.Connection, today: date) -> SourceItem:
        due = int(conn.execute(_DUE, (today.isoformat(),)).fetchone()["n"])
        # Deterministic on purpose — see anki.py._due_item.
        occurred_at = f"{today.isoformat()}T00:00:00+00:00"
        body = f"{due} cards due"
        title = f"Avorio due · {today.isoformat()}"
        return SourceItem(
            source=self.name,
            external_id=f"due:{today.isoformat()}:{due}",
            occurred_at=occurred_at,
            author=self.name,
            title=title,
            body_text=body,
            raw_json=json.dumps({"date": today.isoformat(), "due": due}, sort_keys=True),
            content_hash=content_hash(
                author=self.name, title=title, body_text=body, occurred_at=occurred_at
            ),
        )

    # ── plumbing ─────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        # mode=ro, NOT immutable=1 — the store is WAL and immutable skips the -wal
        # file. See anki.py's module docstring.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    def _today(self) -> date:
        return datetime.now(tz=ZoneInfo(self.tz)).date()

    def _local_day(self, reviewed_at: str) -> str:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)  # datetime('now') writes UTC, naive
        return parsed.astimezone(ZoneInfo(self.tz)).date().isoformat()


def _schema_gaps(conn: sqlite3.Connection) -> list[str]:
    gaps: list[str] = []
    for table, columns in _REQUIRED.items():
        have = {
            str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not have:
            gaps.append(f"table {table}")
            continue
        gaps.extend(f"{table}.{c}" for c in columns if c not in have)
    return gaps


def _utc_iso(reviewed_at: str) -> str:
    parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.replace(microsecond=0).isoformat()


def _parse(cursor: Cursor) -> tuple[str, str]:
    """``{"reviewed_at": str, "due_date": str}``; anything unreadable is a full scan."""
    try:
        state = json.loads(str(cursor or ""))
        return str(state["reviewed_at"]), str(state.get("due_date", ""))
    except (ValueError, TypeError, KeyError):
        return "", ""
