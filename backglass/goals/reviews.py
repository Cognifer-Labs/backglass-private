"""Spaced-repetition wiring.

The anki/avorio connectors write two source_item shapes — per-day review tallies
(``reviews:<date>:...``) and a once-a-day due snapshot (``due:<date>``). This module
is the deterministic layer that turns those into ledger effects. No model is ever
called here: the data arrives structured, so a model call would be spend with
nothing to infer.

Three consumers, all reading the same rows:

  * `sync_checkpoints` — runs inside `backglass.sync` after extraction. Each
    review-day item becomes one checkpoint on the configured cadence target
    (`REVIEWS_TARGET_ID`), `source='extraction'` with the item as provenance (G9).
    A day already counted gets a zero-delta marker for later batches of the same
    day, so a partial-day sync followed by an evening sync counts one day once.
  * `review_minutes` — feeds the capacity model: today's due count × the owner's
    own trailing seconds-per-card, measured from their review history rather than
    guessed. The fallback before any history exists is deliberately conservative.
  * `streak` — consecutive review days ending today or yesterday, for the brief
    line. Quiet number, C4: no flames, no guilt copy.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from backglass.config import Settings
from backglass.goals import checkpoints

#: Before any measured history exists. ~8s/card is the observed premed mix; being a
#: little high costs a few reserved minutes, being low over-promises the day (the
#: docs/04 §1.2 direction: under-promise capacity).
DEFAULT_SECONDS_PER_CARD = 8.0

#: How many trailing review-day tallies inform the per-card estimate.
TRAILING_DAYS = 14

#: Bounds on the measured pace. Review apps count wall-clock per card, which
#: includes walking away from an open card — the owner's real store showed a
#: "155 min for 8 reviews" day that, unclamped, drove the whole day's capacity to
#: zero. Under 2s is a mis-tap, over 60s is idle time, not review time.
MIN_SECONDS_PER_CARD = 2.0
MAX_SECONDS_PER_CARD = 60.0

_SOURCES = ("anki", "avorio")


def sync_checkpoints(conn: sqlite3.Connection, settings: Settings) -> int:
    """Write checkpoints for review-day items that have none yet. Returns rows
    written (counted as domain writes — the idempotency rule applies: a second run
    over unchanged items writes zero)."""
    target_id = settings.reviews_target_id
    if not target_id:
        return 0
    if (
        conn.execute(
            "SELECT 1 FROM target WHERE id = ? AND active = 1", (target_id,)
        ).fetchone()
        is None
    ):
        return 0  # a stale config id degrades to ingest-only, never a crash (rule 5)

    rows = conn.execute(
        "SELECT si.id, si.raw_json FROM source_item si "
        "WHERE si.source IN (?, ?) AND si.external_id LIKE 'reviews:%' "
        "AND NOT EXISTS (SELECT 1 FROM checkpoint c "
        "                WHERE c.source_item_id = si.id AND c.target_id = ?) "
        "ORDER BY si.occurred_at, si.id",
        (*_SOURCES, target_id),
    ).fetchall()

    written = 0
    for row in rows:
        payload = json.loads(row["raw_json"] or "{}")
        day = str(payload.get("date") or "")
        if not day:
            continue
        # Only extraction checkpoints mark a day as counted — a manual checkpoint
        # the owner happened to log that day is their action, not this tally's.
        counted = conn.execute(
            "SELECT 1 FROM checkpoint WHERE target_id = ? AND delta > 0 "
            "AND source = 'extraction' AND date(occurred_at) = ?",
            (target_id, day),
        ).fetchone()
        checkpoints.record(
            conn,
            target_id,
            source="extraction",
            # Noon keeps the checkpoint inside the local day the tally belongs to,
            # whatever zone later reads it in. The item itself carries the exact time.
            occurred_at=f"{day}T12:00:00",
            source_item_id=int(row["id"]),
            note=f"{payload.get('reviews', 0)} reviews · {payload.get('minutes', 0)} min",
            delta=0 if counted else 1,
        )
        written += 1
    return written


def due_snapshot(conn: sqlite3.Connection, day: date) -> dict[str, Any] | None:
    """Today's due load with its provenance, summed across apps when both report.

    Only snapshots taken *for* this day count — yesterday's number is yesterday's.
    The count is part of the snapshot's external id (``due:<date>:<count>``, which
    is what makes the item immutable-safe), so a rescan can leave siblings for one
    date; the newest row per source wins.
    """
    rows = conn.execute(
        "SELECT id, source, external_id, occurred_at, title, raw_json "
        "FROM source_item WHERE source IN (?, ?) AND external_id LIKE ? "
        "ORDER BY id",
        (*_SOURCES, f"due:{day.isoformat()}:%"),
    ).fetchall()
    if not rows:
        return None
    latest_per_source = {str(row["source"]): row for row in rows}
    total = 0
    for row in latest_per_source.values():
        payload = json.loads(row["raw_json"] or "{}")
        total += int(payload.get("due") or 0)
    first = next(iter(latest_per_source.values()))
    return {
        "due": total,
        "date": day.isoformat(),
        "item_id": int(first["id"]),
        "source": str(first["source"]),
        "external_id": str(first["external_id"]),
        "occurred_at": str(first["occurred_at"]),
        "title": str(first["title"]),
        "apps": len(rows),
    }


def trailing_seconds_per_card(conn: sqlite3.Connection, day: date) -> float:
    """The owner's own pace, from their last `TRAILING_DAYS` review-day tallies."""
    since = (day - timedelta(days=TRAILING_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT raw_json FROM source_item "
        "WHERE source IN (?, ?) AND external_id LIKE 'reviews:%' "
        "AND substr(external_id, 9, 10) >= ?",
        (*_SOURCES, since),
    ).fetchall()
    reviews = 0
    minutes = 0
    for row in rows:
        payload = json.loads(row["raw_json"] or "{}")
        reviews += int(payload.get("reviews") or 0)
        minutes += int(payload.get("minutes") or 0)
    if reviews == 0 or minutes == 0:
        return DEFAULT_SECONDS_PER_CARD
    pace = (minutes * 60) / reviews
    return min(max(pace, MIN_SECONDS_PER_CARD), MAX_SECONDS_PER_CARD)


def review_minutes(conn: sqlite3.Connection, day: date) -> tuple[int, dict[str, Any] | None]:
    """(minutes the day's due load will take, the snapshot that claims it)."""
    snapshot = due_snapshot(conn, day)
    if snapshot is None or not snapshot["due"]:
        return 0, snapshot
    pace = trailing_seconds_per_card(conn, day)
    return round(int(snapshot["due"]) * pace / 60), snapshot


def streak(conn: sqlite3.Connection, settings: Settings, day: date) -> int:
    """Consecutive review days ending today or yesterday. Today not yet reviewed
    does not break the streak — the day is not over."""
    target_id = settings.reviews_target_id
    if not target_id:
        return 0
    probe = day
    if not _reviewed_on(conn, target_id, probe):
        probe = probe - timedelta(days=1)
    count = 0
    while _reviewed_on(conn, target_id, probe):
        count += 1
        probe = probe - timedelta(days=1)
    return count


def _reviewed_on(conn: sqlite3.Connection, target_id: int, day: date) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM checkpoint WHERE target_id = ? AND delta > 0 "
            "AND source = 'extraction' AND date(occurred_at) = ?",
            (target_id, day.isoformat()),
        ).fetchone()
        is not None
    )
