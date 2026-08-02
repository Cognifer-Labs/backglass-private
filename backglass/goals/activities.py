"""The activity registry.

A discrete extracurricular — organization, role, supervisor, date range — that hour
checkpoints attach to, so the AMCAS Work & Activities section can later be assembled
from evidence instead of memory. Categories map to the medical preset's total keys
plus `other`; the mapping is a plain key rather than a foreign key so activities
survive a roadmap being dropped and re-instantiated.

Per-activity hours are `SUM(delta)` over the activity's checkpoints on total targets,
computed on read (G3, G10): deleting a checkpoint recomputes the activity's hours the
same way it recomputes the target's. Only total-target checkpoints count — those carry
hours; a cadence tick that happens to name an activity is a session, not an hour figure.

AMCAS allows 15 activities and 3 "most meaningful". Both are surfaced as counts and
never enforced — AMCAS enforces its own caps, and refusing a 16th row here would only
push the overflow back into a spreadsheet.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from backglass.db import now_iso
from backglass.ledger import USER_ID

CATEGORIES = ("shadowing", "clinical", "volunteering", "research", "leadership", "other")

AMCAS_SLOTS = 15
AMCAS_MOST_MEANINGFUL = 3
AMCAS_DESCRIPTION_CHARS = 700
AMCAS_MEANINGFUL_CHARS = 1325


#: Which total-target a category's hours belong to, matched against the target title.
#:
#: The preset carries an explicit key per total (`"key": "shadowing"`), but
#: `roadmap/instantiate.add_total` writes only the title — the key is dropped, and
#: `target` has no column for it. So the link from an AMCAS category to the accumulator
#: it feeds has to be rebuilt on read, and this table is that rebuild. It is keyword
#: based rather than exact because the titles are prose the owner may edit ("Shadowing
#: hours (3+ specialties, ≥1 primary care)").
#:
#: This is a workaround for a schema gap, not the end state — a `preset_key` column on
#: `target`, backfilled by these same hints, would make the link explicit and survive a
#: retitle. Left as follow-up rather than done inline because it needs a migration.
#:
#: Each entry is (must contain any of, must contain none of). The exclusions are not
#: hypothetical: the shipped preset titles are "Clinical experience hours (paid or
#: volunteer)" and "Non-clinical service hours", so a naive substring match files
#: volunteering hours under clinical (the parenthetical says "volunteer") and clinical
#: hours under volunteering (the other title contains "clinical"). Parentheticals are
#: stripped before matching for the same reason — a qualifier in brackets describes the
#: target, it does not name the category.
CATEGORY_TITLE_HINTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "shadowing": (("shadow",), ()),
    "clinical": (("clinical",), ("non-clinical", "nonclinical")),
    "volunteering": (("volunteer", "service"), ()),
    "research": (("research",), ()),
    "leadership": (("leadership", "teaching"), ()),
}


class ActivityError(ValueError):
    pass


def add(
    conn: sqlite3.Connection,
    *,
    title: str,
    org: str | None = None,
    role: str | None = None,
    category: str = "other",
    contact_entity_id: int | None = None,
    started_on: str | None = None,
) -> int:
    title = title.strip()
    if not title:
        raise ActivityError("an activity needs a title")
    if category not in CATEGORIES:
        raise ActivityError(
            f"unknown category {category!r}; expected one of {CATEGORIES}"
        )
    conn.execute(
        "INSERT INTO activity (user_id, title, org, role, category, contact_entity_id, "
        " started_on, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            USER_ID,
            title,
            (org or "").strip() or None,
            (role or "").strip() or None,
            category,
            contact_entity_id,
            started_on,
            now_iso(),
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def toggle_most_meaningful(conn: sqlite3.Connection, activity_id: int) -> bool:
    row = _get(conn, activity_id)
    flag = 0 if row["most_meaningful"] else 1
    conn.execute("UPDATE activity SET most_meaningful = ? WHERE id = ?", (flag, activity_id))
    return bool(flag)


def end(conn: sqlite3.Connection, activity_id: int, ended_on: str) -> None:
    _get(conn, activity_id)
    conn.execute(
        "UPDATE activity SET ended_on = ?, is_ongoing = 0 WHERE id = ?",
        (ended_on, activity_id),
    )


def _get(conn: sqlite3.Connection, activity_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM activity WHERE id = ? AND user_id = ? AND active = 1",
        (activity_id, USER_ID),
    ).fetchone()
    if row is None:
        raise ActivityError(f"no activity {activity_id}")
    return row  # type: ignore[no-any-return]


def list_with_hours(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every active activity with its evidence summary: hours (total-target
    checkpoints only), entry count, and the span of logged dates."""
    rows = conn.execute(
        "SELECT a.*, e.canonical_name AS contact_name, "
        "  COALESCE((SELECT SUM(c.delta) FROM checkpoint c "
        "            JOIN target t ON t.id = c.target_id AND t.kind = 'total' "
        "            WHERE c.activity_id = a.id), 0) AS hours, "
        "  (SELECT COUNT(*) FROM checkpoint c "
        "   JOIN target t ON t.id = c.target_id AND t.kind = 'total' "
        "   WHERE c.activity_id = a.id) AS entry_count, "
        "  (SELECT MIN(c.occurred_at) FROM checkpoint c WHERE c.activity_id = a.id) "
        "   AS first_logged, "
        "  (SELECT MAX(c.occurred_at) FROM checkpoint c WHERE c.activity_id = a.id) "
        "   AS last_logged "
        "FROM activity a LEFT JOIN entity e ON e.id = a.contact_entity_id "
        "WHERE a.user_id = ? AND a.active = 1 "
        "ORDER BY a.most_meaningful DESC, a.category, a.id",
        (USER_ID,),
    ).fetchall()
    return [dict(r) for r in rows]


def find_by_name(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    """Activities whose title or org matches `name`, best match first.

    Logging an hour should cost one line, and one line means naming the activity the
    way the owner thinks of it ("lab", "Dr. Chen") rather than by id. An exact
    case-insensitive title match wins outright; otherwise every substring hit is
    returned so the caller can ask which one instead of guessing — filing four years of
    research hours under the wrong activity is not a recoverable mistake.
    """
    needle = name.strip().lower()
    if not needle:
        return []
    rows = [a for a in list_with_hours(conn)]
    exact = [a for a in rows if str(a["title"]).lower() == needle]
    if exact:
        return exact
    return [
        a
        for a in rows
        if needle in str(a["title"]).lower() or needle in str(a["org"] or "").lower()
    ]


def total_target_for(
    conn: sqlite3.Connection, category: str, goal_id: int | None = None
) -> dict[str, Any] | None:
    """The lifetime accumulator a category's hours feed, or None if there isn't one.

    See CATEGORY_TITLE_HINTS for why this is a title match rather than a join. `other`
    has no accumulator by design — it is the escape hatch for an activity that belongs
    in the AMCAS list but under no hour category, and inventing a target for it would
    put a number on the roadmap page that means nothing.
    """
    hint = CATEGORY_TITLE_HINTS.get(category)
    if hint is None:
        return None
    wanted, excluded = hint
    sql = (
        "SELECT t.id, t.title, t.total_count, t.goal_id, "
        "  COALESCE((SELECT SUM(c.delta) FROM checkpoint c WHERE c.target_id = t.id), 0)"
        "  AS done "
        "FROM target t WHERE t.kind = 'total' AND t.active = 1"
    )
    params: list[Any] = []
    if goal_id is not None:
        sql += " AND t.goal_id = ?"
        params.append(goal_id)
    sql += " ORDER BY t.id"
    for row in conn.execute(sql, params).fetchall():
        # Parentheticals are qualifiers, not category names: "(paid or volunteer)" on
        # the clinical total would otherwise claim every volunteering hour.
        title = re.sub(r"\([^)]*\)", " ", str(row["title"])).lower()
        if any(bad in title for bad in excluded):
            continue
        if any(want in title for want in wanted):
            return dict(row)
    return None


@dataclass(frozen=True)
class LoggedHours:
    """What one log entry did, so the caller can say it back without re-querying."""

    activity_id: int
    activity_title: str
    target_id: int
    target_title: str
    hours: int
    target_done: int
    target_total: int


def log_hours(
    conn: sqlite3.Connection,
    *,
    activity_id: int,
    hours: int,
    occurred_at: str,
    note: str | None = None,
) -> LoggedHours:
    """File `hours` against an activity and the accumulator its category feeds.

    The whole point is that this costs one line. The activity ledger is the part of a
    pre-med record that cannot be reconstructed later — four years of hours are not
    recoverable from memory or from mail — and it stays empty exactly as long as
    logging means opening a browser, finding the roadmap page and filling a form.

    One log entry is still one checkpoint, written the same way the roadmap route
    writes it (G9: every checkpoint says what produced it), so nothing here is a second
    source of truth. `occurred_at` is passed in rather than read from the clock because
    dates.py's rule holds everywhere: the caller knows which local day this belongs to.
    """
    if hours <= 0:
        raise ActivityError("hours must be positive")
    activity = _get(conn, activity_id)
    target = total_target_for(conn, str(activity["category"]))
    if target is None:
        raise ActivityError(
            f"{activity['title']!r} is category {activity['category']!r}, which has no "
            "lifetime hour target on any active goal — log it on the roadmap page "
            "against a specific total, or recategorize the activity"
        )
    from backglass.goals import checkpoints

    checkpoints.record(
        conn,
        int(target["id"]),
        source="manual",
        occurred_at=occurred_at,
        note=note,
        delta=hours,
        activity_id=activity_id,
    )
    return LoggedHours(
        activity_id=activity_id,
        activity_title=str(activity["title"]),
        target_id=int(target["id"]),
        target_title=str(target["title"]),
        hours=hours,
        target_done=int(target["done"]) + hours,
        target_total=int(target["total_count"] or 0),
    )


def entries_for(conn: sqlite3.Connection, activity_id: int) -> list[dict[str, Any]]:
    """The full checkpoint-note stream for one activity — the raw material for its
    AMCAS description, each line traceable to the checkpoint it came from (rule 1)."""
    rows = conn.execute(
        "SELECT c.id, c.occurred_at, c.delta, c.note, t.title AS target_title, t.kind "
        "FROM checkpoint c JOIN target t ON t.id = c.target_id "
        "WHERE c.activity_id = ? ORDER BY c.occurred_at, c.id",
        (activity_id,),
    ).fetchall()
    return [dict(r) for r in rows]
