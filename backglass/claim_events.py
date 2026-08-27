"""The change ledger. Migration 0032; tasks/pipeline-redesign-2026-08-21.md §2.3/§3.3.

Every belief-layer write in this pipeline used to leave no trace of *what changed*, only
the new state. Four mechanisms grew independently to work around that absence:
`day_plan.inputs_fingerprint` hashes the whole world to detect drift, `logic_check` is
judged once per commitment with no way to notice a fact moved, goal 3 planned
`commitment_dependency` as an invalidation index and never built it, and the immutable
`source_item` conflict class re-announces the same upstream change forever because
nothing records that it was already seen once.

`claim_event` is the append-only log a writer emits alongside its own UPDATE. Nothing
reads it as a source of truth — the typed tables stay primary, exactly as CLAUDE.md
requires — but it is the one place a caller can ask "what changed, and why" without
re-deriving it from a diff of two reads.

`claim_dependency` is what changed. A claim (a commitment, eventually an engagement or a
plan) can name what its current standing rests on: a fact, or explicitly `none`. When
that fact supersedes or retracts, its dependents are looked up here — this is the
invalidation index goal 3's own docstring says is missing: "nothing else in the ledger
can answer 'which obligations did this fact hold up?'"

Two rules, both load-bearing:

- **Additive, not load-bearing** (the same boundary CLAUDE.md draws for retrieval).
  Wiring into a given writer is incremental — see the migration's own note on which
  writers are wired as of 0032. A subject with no recorded dependency is not a subject
  with no dependency; it may simply not be wired yet, and every reader must treat an
  empty `claim_dependency` result as "unknown", never as "confirmed independent".
- **Append-only.** Nothing here is ever UPDATEd except `claim_dependency.status`, which
  moves active → superseded exactly once, pointing at the `claim_event` that caused it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from backglass.db import now_iso
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class Dependency:
    id: int
    subject_table: str
    subject_id: int
    dep_key: str
    kind: str
    fact_id: int | None
    quote: str | None
    reason: str
    status: str


def record(
    conn: sqlite3.Connection,
    *,
    subject_table: str,
    subject_id: int,
    cause: str,
    field: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    at: str | None = None,
) -> int:
    """One row in the change ledger. Returns its id.

    `at` is only ever passed by a test pinning the clock — production always reads
    `now_iso()`, the same clock every other write in this pipeline uses.
    """
    cur = conn.execute(
        "INSERT INTO claim_event (user_id, at, subject_table, subject_id, field, "
        " old_value, new_value, cause) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            USER_ID,
            at or now_iso(),
            subject_table,
            subject_id,
            field,
            old_value,
            new_value,
            cause,
        ),
    )
    return int(cur.lastrowid or 0)


def since(
    conn: sqlite3.Connection,
    subject_table: str,
    subject_id: int,
    *,
    after_id: int = 0,
) -> list[sqlite3.Row]:
    """This subject's history, oldest first. `after_id` bounds a diff between two reads."""
    return list(
        conn.execute(
            "SELECT * FROM claim_event WHERE user_id = ? AND subject_table = ? "
            " AND subject_id = ? AND id > ? ORDER BY id ASC",
            (USER_ID, subject_table, subject_id, after_id),
        )
    )


def recent(conn: sqlite3.Connection, *, since_at: str, limit: int = 50) -> list[sqlite3.Row]:
    """Everything the ledger changed since a timestamp — the state doc's "what changed"."""
    return list(
        conn.execute(
            "SELECT * FROM claim_event WHERE user_id = ? AND at >= ? "
            "ORDER BY id ASC LIMIT ?",
            (USER_ID, since_at, limit),
        )
    )


def depends_on_fact(
    conn: sqlite3.Connection,
    *,
    subject_table: str,
    subject_id: int,
    fact_id: int,
    quote: str,
    reason: str,
) -> int:
    """Record (or refresh) that a claim's standing rests on one fact.

    Upsert on `(subject_table, subject_id, dep_key)`: a claim depends on at most one
    current verdict about a given fact, and re-recording the same dependency (a repeat
    judgment that reaches the same conclusion) must not create a second active row —
    rule 3, applied to a table that is not itself the ledger.
    """
    dep_key = f"fact:{fact_id}"
    cur = conn.execute(
        "INSERT INTO claim_dependency (user_id, subject_table, subject_id, dep_key, kind, "
        " fact_id, quote, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, 'fact', ?, ?, ?, 'active', ?) "
        "ON CONFLICT (user_id, subject_table, subject_id, dep_key) "
        "DO UPDATE SET quote = excluded.quote, reason = excluded.reason, "
        " status = 'active', superseded_by = NULL",
        (USER_ID, subject_table, subject_id, dep_key, fact_id, quote, reason, now_iso()),
    )
    return int(cur.lastrowid or 0)


def depends_on_none(
    conn: sqlite3.Connection,
    *,
    subject_table: str,
    subject_id: int,
    reason: str,
) -> int:
    """Record that a claim was judged and depends on no recorded fact.

    First-class, not a default: goal 3's own reasoning is that forcing a dependency onto
    a claim that has none would invent the citation the citation-or-no-verdict rule
    exists to prevent.
    """
    cur = conn.execute(
        "INSERT INTO claim_dependency (user_id, subject_table, subject_id, dep_key, kind, "
        " reason, status, created_at) VALUES (?, ?, ?, 'none', 'none', ?, 'active', ?) "
        "ON CONFLICT (user_id, subject_table, subject_id, dep_key) "
        "DO UPDATE SET reason = excluded.reason, status = 'active', superseded_by = NULL",
        (USER_ID, subject_table, subject_id, reason, now_iso()),
    )
    return int(cur.lastrowid or 0)


def active_dependencies(
    conn: sqlite3.Connection, subject_table: str, subject_id: int
) -> list[Dependency]:
    rows = conn.execute(
        "SELECT * FROM claim_dependency WHERE user_id = ? AND subject_table = ? "
        " AND subject_id = ? AND status = 'active' ORDER BY id ASC",
        (USER_ID, subject_table, subject_id),
    ).fetchall()
    return [_dependency(r) for r in rows]


def dependents_of_fact(conn: sqlite3.Connection, fact_id: int) -> list[Dependency]:
    """Everything whose standing currently rests on this fact — the invalidation lookup."""
    rows = conn.execute(
        "SELECT * FROM claim_dependency WHERE user_id = ? AND fact_id = ? "
        " AND status = 'active' ORDER BY id ASC",
        (USER_ID, fact_id),
    ).fetchall()
    return [_dependency(r) for r in rows]


def invalidate_fact(
    conn: sqlite3.Connection, fact_id: int, *, event_id: int
) -> list[Dependency]:
    """A fact left `active` (superseded or retracted): break what depended on it.

    `event_id` is the `claim_event` row the caller already wrote for the fact's own
    status change — this function does not write one, so a fact with no dependents
    still gets exactly one event for its own transition, never zero and never two.

    Every affected dependency is marked `superseded`, pointed at that event, and the
    dependent subject gets its own event so a reader following *that* subject's history
    sees why its standing changed — the record a re-judgment pass keys its invalidation
    off, once one exists.

    Returns the dependencies that were broken, for the caller to act on (or, today,
    simply to know about — nothing yet re-judges automatically; see the migration note).
    """
    dependents = dependents_of_fact(conn, fact_id)
    for dep in dependents:
        conn.execute(
            "UPDATE claim_dependency SET status = 'superseded', superseded_by = ? "
            "WHERE id = ?",
            (event_id, dep.id),
        )
        record(
            conn,
            subject_table=dep.subject_table,
            subject_id=dep.subject_id,
            cause=f"dependency_broken:fact:{fact_id}",
            field="dependency",
            old_value=f"fact:{fact_id}",
            new_value=None,
        )
    return dependents


def _dependency(row: sqlite3.Row) -> Dependency:
    return Dependency(
        id=int(row["id"]),
        subject_table=str(row["subject_table"]),
        subject_id=int(row["subject_id"]),
        dep_key=str(row["dep_key"]),
        kind=str(row["kind"]),
        fact_id=int(row["fact_id"]) if row["fact_id"] is not None else None,
        quote=row["quote"],
        reason=str(row["reason"]),
        status=str(row["status"]),
    )
