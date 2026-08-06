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

#: The largest number of hours one entry may carry. More than a year of continuous
#: hours, so it never rejects a real backfill — it exists only to keep an unbounded
#: integer out of a column that is later summed. See log_hours.
MAX_HOURS_PER_ENTRY = 10_000

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
#: Each entry is (must contain any of, must contain none of), matched against a
#: normalized title: parenthetical qualifiers removed, and the
#: phrase "non-clinical" removed as a unit. That second step is what lets `clinical` and
#: `volunteering` be told apart by plain words. The two real titles collide in both
#: directions — "Clinical experience hours (paid or volunteer)" and "Non-clinical service
#: hours" — and so do the retitles an owner might reasonably write ("Clinical hours, paid
#: or volunteer" has no parentheses to strip). Once "non-clinical" is gone, a residual
#: "clinical" means the clinical total and nothing else, so `volunteering` can exclude it
#: without excluding itself.
CATEGORY_TITLE_HINTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "shadowing": (("shadow",), ()),
    "clinical": (("clinical",), ()),
    "volunteering": (("volunteer", "service"), ("clinical",)),
    "research": (("research",), ()),
    "leadership": (("leadership", "teaching"), ()),
}

#: Removed before matching, as a phrase. See CATEGORY_TITLE_HINTS.
_NON_CLINICAL = re.compile(r"non[\s_\-\u2010-\u2015]*clinical")


class ActivityError(ValueError):
    pass


def check_hours(hours: int) -> None:
    """Raise unless `hours` is a plausible amount for one entry.

    Separate from `log_hours` so a caller can refuse *before* it creates anything —
    every reason to reject has to be reachable before the first durable write.

    The upper bound is not typo paranoia: any integer up to 2**63-1 is a legal SQLite
    INTEGER, so an absurd value commits happily and then every later `SUM(delta)` raises
    "integer overflow". That takes out `log`, `amcas-export` and both dashboard pages at
    once, and no CLI path can delete a checkpoint to undo it — recovery means raw SQL.
    The limit sits far above any real entry (more than a year of continuous hours), so
    it can only ever catch a mistake.
    """
    if hours <= 0:
        raise ActivityError("hours must be positive")
    if hours > MAX_HOURS_PER_ENTRY:
        raise ActivityError(
            f"{hours} hours in one entry is not plausible (limit "
            f"{MAX_HOURS_PER_ENTRY}); log the sessions separately"
        )


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
    # Refused here rather than at a caller, because `title` is what every later lookup
    # resolves by and there is no rename or delete for an activity anywhere. A second
    # row with the same name makes that name permanently unloggable — `log` can only
    # report the ambiguity — and splits one activity's hours across two AMCAS entries.
    # The CLI learned this first; the dashboard's add form calls straight through here,
    # so the check has to live at the funnel or it covers one door out of two.
    if _titled(conn, title) is not None:
        raise ActivityError(f"an activity titled {title!r} already exists")
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


def _norm(title: str) -> str:
    """The one way this module decides two titles are the same name.

    `casefold`, not `lower`, and in Python rather than SQL. SQLite's built-in `LOWER()`
    folds ASCII only, so `LOWER('CAFÉ LATINO') != LOWER('Café Latino')` — while
    `find_by_name`, which later has to answer *which* activity a name means, compares
    with Python's full-Unicode fold and calls them identical. The guard and the resolver
    disagreeing is worse than either rule alone: "Café Latino" and "CAFÉ LATINO" both got
    created, and from then on the name matched two rows, so it could never be logged
    against and its hours split across two AMCAS entries. Accented characters are
    ordinary in real activity names, so this was a live path, not a curiosity.
    """
    return title.strip().casefold()


def _titled(conn: sqlite3.Connection, title: str) -> sqlite3.Row | None:
    """An active activity with exactly this title, case-insensitively, or None.

    Exact — not `find_by_name`'s substring search, which also matches orgs. Using that
    here refused "Chen" because "Chen Lab Neuroscience" existed, and said "an activity
    named 'Chen' already exists" when none did. A duplicate guard has to answer "is this
    the same activity", and only an exact title does.
    """
    wanted = _norm(title)
    for row in conn.execute(
        "SELECT * FROM activity WHERE user_id = ? AND active = 1", (USER_ID,)
    ):
        if _norm(str(row["title"])) == wanted:
            return row  # type: ignore[no-any-return]
    return None


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
        "   AS last_logged, "
        # Every checkpoint carrying words, not only the hour-bearing ones: this is the
        # raw material `amcas-export` concatenates into the draft description, and it
        # counts a cadence tick's note the same way the export does.
        "  (SELECT COUNT(*) FROM checkpoint c WHERE c.activity_id = a.id "
        "   AND c.note IS NOT NULL AND TRIM(c.note) != '') AS note_entries "
        "FROM activity a LEFT JOIN entity e ON e.id = a.contact_entity_id "
        "WHERE a.user_id = ? AND a.active = 1 "
        "ORDER BY a.most_meaningful DESC, a.category, a.id",
        (USER_ID,),
    ).fetchall()
    return [dict(r) for r in rows]


#: What `amcas-export` prints a placeholder for, in the order the export prints it.
#: Each key is a field the export renders as `—` when it is missing, and the value is
#: the word the page shows instead. Kept as one table so the two surfaces cannot drift:
#: `tests/test_amcas_readiness.py` asserts every key here still corresponds to a
#: placeholder the export actually emits, and that an activity with none of these gaps
#: produces an export section with no placeholder in it at all.
EXPORT_FIELDS = ("org", "contact", "dates", "notes")


def export_gaps(activity: dict[str, Any]) -> list[str]:
    """What this activity would still be missing in Work & Activities, in export order.

    The CLI has always known this — it prints "Organization: —" and "Contact: — (add a
    supervisor entity)" and "no dates logged" — but the page that is used to *build* the
    record never said it, so a gap was only discoverable by running an export the owner
    had no reason to run until the application was due.

    A gap is not a failure and this is not validation: AMCAS's own limits are surfaced
    and never enforced (see the module docstring), and an activity is perfectly loggable
    with every one of these missing. It is a checklist, printed where the record is made.
    """
    gaps: list[str] = []
    if not (activity.get("org") or "").strip():
        gaps.append("org")
    if not activity.get("contact_name"):
        gaps.append("contact")
    # The export falls back to the logged span when `started_on` is unset, so dates are
    # missing only when neither exists — an activity logged against real dates needs no
    # hand-entered start.
    if not (activity.get("started_on") or activity.get("first_logged")):
        gaps.append("dates")
    # No noted checkpoint means the export's "draft material" is the empty string: hours
    # with nothing to write the description from.
    if not int(activity.get("note_entries") or 0):
        gaps.append("notes")
    return gaps


def find_by_name(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    """Activities whose title or org matches `name`, best match first.

    Logging an hour should cost one line, and one line means naming the activity the
    way the owner thinks of it ("lab", "Dr. Chen") rather than by id. An exact
    case-insensitive title match wins outright; otherwise every substring hit is
    returned so the caller can ask which one instead of guessing — filing four years of
    research hours under the wrong activity is not a recoverable mistake.
    """
    needle = _norm(name)
    if not needle:
        return []
    rows = list_with_hours(conn)
    exact = [a for a in rows if _norm(str(a["title"])) == needle]
    if exact:
        return exact
    return [
        a
        for a in rows
        if needle in _norm(str(a["title"])) or needle in _norm(str(a["org"] or ""))
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
    # Only targets on a live goal. An archived goal keeps its target rows, and without
    # this an abandoned path's "Research hours" could outrank the active one purely by
    # having a lower id.
    sql = (
        "SELECT t.id, t.title, t.total_count, t.goal_id, "
        "  COALESCE((SELECT SUM(c.delta) FROM checkpoint c WHERE c.target_id = t.id), 0)"
        "  AS done "
        "FROM target t JOIN goal g ON g.id = t.goal_id "
        "WHERE t.kind = 'total' AND t.active = 1 AND g.status = 'active'"
    )
    params: list[Any] = []
    if goal_id is not None:
        sql += " AND t.goal_id = ?"
        params.append(goal_id)
    sql += " ORDER BY t.id"
    for row in conn.execute(sql, params).fetchall():
        # Parentheticals are qualifiers, not category names: "(paid or volunteer)" on
        # the clinical total would otherwise claim every volunteering hour. "non-clinical"
        # goes as a phrase so a residual "clinical" unambiguously means the clinical total.
        title = re.sub(r"\([^)]*\)", " ", str(row["title"])).lower()
        title = _NON_CLINICAL.sub(" ", title)
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
    check_hours(hours)
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
