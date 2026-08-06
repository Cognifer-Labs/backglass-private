"""Write-back. docs/06 §Write-back is required.

    "If the dashboard is read-only it becomes decoration within a week. This is not a
    nice-to-have; a surface you cannot act on is a surface you stop opening."

The seven actions docs/06 lists, each one function. They are here rather than inline in
the routes so that the route layer stays about HTTP and fragments, and so these can be
tested without a client.

docs/11 §Cross-cutting rule 2 — "proposals, not actions" — bounds what these may do:
nothing here writes to a calendar, sends mail, or resolves anything on the owner's behalf.
Every one of them is the owner's own click.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan import timezones

#: docs/11 §4: "Rejecting also records the reason category, one click: wrong date, not a
#: commitment, not mine, already done. Four buttons, no free text."
#:
#: "Those categories are the feedback loop. After fifty rejections, the distribution tells
#: you which part of the extraction prompt to fix." Free text would not aggregate, which
#: is the whole reason it is four buttons.
REJECT_REASONS = {
    "wrong_date": "wrong date",
    "not_a_commitment": "not a commitment",
    "not_mine": "not mine",
    "already_done": "already done",
}

#: docs/04 C1, enforced in application code rather than in the schema so the error can
#: explain itself.
CHECKLIST_CAP = 7


class ActionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    ok: bool
    detail: str = ""


def _require_open(conn: sqlite3.Connection, commitment_id: int) -> dict[str, Any]:
    row: dict[str, Any] | None = conn.execute(
        "SELECT * FROM commitment WHERE id = ? AND user_id = ?", (commitment_id, USER_ID)
    ).fetchone()
    if row is None:
        raise ActionError(f"no commitment {commitment_id}")
    # Enforced here, not by each caller's `WHERE status = 'open'`: that clause made a
    # write on a closed row match zero rows and still report ok — a stale page's
    # snooze answered "snoozed 1d" having snoozed nothing. Refusing turns it into a
    # 422 the failed-write strip can show.
    if str(row["status"]) != "open":
        raise ActionError(f"commitment {commitment_id} is already {row['status']}")
    return row


# ── 1. resolve, drop, snooze ──────────────────────────────────────────────


def resolve(conn: sqlite3.Connection, commitment_id: int, note: str | None = None) -> Result:
    """Mark done. docs/11 §3: "no confirmation on resolve — the action is reversible"."""
    _require_open(conn, commitment_id)
    conn.execute(
        "UPDATE commitment SET status = 'done', resolved_at = ?, resolution_note = ? "
        "WHERE id = ? AND status = 'open'",
        (now_iso(), note, commitment_id),
    )
    return Result(ok=True, detail="done")


def drop(conn: sqlite3.Connection, commitment_id: int, note: str | None = None) -> Result:
    """The one destructive action, and the only one docs/06 allows a confirmation on.

    Tombstoned rather than deleted (docs/03 §Retention), so a later re-extraction of the
    same source cannot resurrect it.
    """
    _require_open(conn, commitment_id)
    conn.execute(
        "UPDATE commitment SET status = 'dropped', resolved_at = ?, resolution_note = ? "
        "WHERE id = ? AND status = 'open'",
        (now_iso(), note, commitment_id),
    )
    return Result(ok=True, detail="dropped")


def snooze(conn: sqlite3.Connection, commitment_id: int, days: int = 1) -> Result:
    """Push the due date out. Stays open — a snooze is not a resolution.

    `rollover_count` is incremented because a snooze is a rollover by hand, and docs/05 §7
    surfaces the third one with the drop-or-do question. Snoozing something four times
    without noticing is exactly what that question exists to interrupt.
    """
    row = _require_open(conn, commitment_id)
    if days < 1:
        raise ActionError("snooze must be at least one day")
    base = str(row["due_at"] or now_iso())[:10]
    conn.execute(
        "UPDATE commitment SET due_at = date(?, ?), rollover_count = rollover_count + 1 "
        "WHERE id = ? AND status = 'open'",
        (base, f"+{days} days", commitment_id),
    )
    return Result(ok=True, detail=f"snoozed {days}d")


# ── 2. accept / reject a review-queue item ────────────────────────────────


def accept(conn: sqlite3.Connection, commitment_id: int) -> Result:
    """docs/11 §4: "Accept promotes it to a normal commitment."

    Promotion means raising confidence to certainty: the owner has now confirmed it, and
    a human confirmation is better evidence than any model score. The original score is
    kept in the resolution note so the eval loop can still see what the model said.
    """
    row = _require_open(conn, commitment_id)
    conn.execute(
        "UPDATE commitment SET confidence = 1.0, resolution_note = ? WHERE id = ?",
        (f"accepted by owner (model said {float(row['confidence']):.2f})", commitment_id),
    )
    return Result(ok=True, detail="accepted")


def reject(conn: sqlite3.Connection, commitment_id: int, reason: str) -> Result:
    """docs/11 §4: "Reject tombstones it so re-extraction does not resurrect it."

    The reason category is stored in `resolution_note` in a parseable form, because it is
    the feedback loop: the distribution over fifty rejections says which part of the
    extraction prompt is wrong.
    """
    if reason not in REJECT_REASONS:
        raise ActionError(
            f"unknown reject reason {reason!r}; expected one of {sorted(REJECT_REASONS)}"
        )
    _require_open(conn, commitment_id)
    conn.execute(
        "UPDATE commitment SET status = 'dropped', resolved_at = ?, resolution_note = ? "
        "WHERE id = ?",
        (now_iso(), f"rejected:{reason}", commitment_id),
    )
    return Result(ok=True, detail=REJECT_REASONS[reason])


def accept_plan(conn: sqlite3.Connection, engagement_id: int) -> Result:
    """The engagement half of Accept. Same act, same reasoning as `accept` above:
    the owner confirming a plan is better evidence than any model score."""
    row = conn.execute(
        "SELECT confidence, status FROM engagement WHERE user_id = ? AND id = ?",
        (USER_ID, engagement_id),
    ).fetchone()
    if row is None:
        raise ActionError(f"no plan {engagement_id}")
    if str(row["status"]) == "declined":
        raise ActionError("that plan was already declined")
    conn.execute("UPDATE engagement SET confidence = 1.0 WHERE id = ?", (engagement_id,))
    return Result(ok=True, detail="accepted")


def reject_plan(conn: sqlite3.Connection, engagement_id: int) -> Result:
    """Reject tombstones a plan by declining it.

    `declined` already means "the owner is not doing this" and is already the status the
    dedup pass can see, so re-extraction cannot resurrect it — which is exactly the
    property docs/11 §4 asks a rejection to have, reached with no new column. There are no
    reason categories here, unlike a commitment's four: a plan can be wrong in only one
    interesting way ("that was not a plan"), and inventing categories nobody chooses is
    the mistake the commitment queue already made once and had removed.
    """
    row = conn.execute(
        "SELECT status FROM engagement WHERE user_id = ? AND id = ?",
        (USER_ID, engagement_id),
    ).fetchone()
    if row is None:
        raise ActionError(f"no plan {engagement_id}")
    conn.execute(
        "UPDATE engagement SET status = 'declined', resolved_at = ? WHERE id = ?",
        (now_iso(), engagement_id),
    )
    return Result(ok=True, detail="not a plan")


# ── 3. tick / untick a checklist item ─────────────────────────────────────


def _require_checklist_item(conn: sqlite3.Connection, item_id: int) -> None:
    """Same shape as `_require_open`: a stale page's tick must refuse, not lie.

    Without this, ticking a vanished id was the one 500 in the whole route table
    (the FK on checklist_tick raised IntegrityError past the ActionError catch),
    and unticking one deleted nothing and still said "unticked"."""
    row = conn.execute(
        "SELECT active FROM checklist_item WHERE id = ? AND user_id = ?",
        (item_id, USER_ID),
    ).fetchone()
    if row is None:
        raise ActionError(f"no checklist item {item_id}")
    if not row["active"]:
        raise ActionError(f"checklist item {item_id} is no longer on the checklist")


def tick(conn: sqlite3.Connection, item_id: int, local_date: str) -> Result:
    """Binary, idempotent, and unique per (item, day) at the schema level."""
    _require_checklist_item(conn, item_id)
    conn.execute(
        "INSERT INTO checklist_tick (checklist_item_id, local_date, ticked_at) "
        "VALUES (?, ?, ?) ON CONFLICT (checklist_item_id, local_date) DO NOTHING",
        (item_id, local_date, now_iso()),
    )
    return Result(ok=True, detail="ticked")


def untick(conn: sqlite3.Connection, item_id: int, local_date: str) -> Result:
    _require_checklist_item(conn, item_id)
    conn.execute(
        "DELETE FROM checklist_tick WHERE checklist_item_id = ? AND local_date = ?",
        (item_id, local_date),
    )
    return Result(ok=True, detail="unticked")


def add_checklist_item(conn: sqlite3.Connection, title: str) -> Result:
    """docs/04 C1: the seven-item cap, with an error that explains itself."""
    count = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM checklist_item WHERE user_id = ? AND active = 1",
            (USER_ID,),
        ).fetchone()["n"]
    )
    if count >= CHECKLIST_CAP:
        raise ActionError(
            f"the checklist is capped at {CHECKLIST_CAP} items and has {count}. "
            "A list of non-negotiables that runs to eight has stopped being a list of "
            "non-negotiables. Deactivate one first."
        )
    conn.execute(
        "INSERT INTO checklist_item (user_id, title, sort_order) VALUES (?, ?, ?)",
        (USER_ID, title, count),
    )
    return Result(ok=True, detail="added")


# ── 4 & 5. plan blocks: outcome, and pinning ──────────────────────────────


def set_block_outcome(conn: sqlite3.Connection, block_id: int, outcome: str) -> Result:
    """docs/06: "Mark a plan block done or rolled."."""
    if outcome not in ("pending", "done", "rolled", "dropped"):
        raise ActionError(f"unknown outcome {outcome!r}")
    updated = conn.execute(
        "UPDATE plan_block SET outcome = ?, rollover_count = rollover_count + ? WHERE id = ?",
        (outcome, 1 if outcome == "rolled" else 0, block_id),
    )
    if not updated.rowcount:
        raise ActionError(f"no plan block {block_id}")
    return Result(ok=True, detail=outcome)


def pin_block(conn: sqlite3.Connection, block_id: int, pinned: bool = True) -> Result:
    """docs/06: "Pin a block to a slot." A pinned block is one the planner may not move."""
    updated = conn.execute(
        "UPDATE plan_block SET pinned = ? WHERE id = ?", (1 if pinned else 0, block_id)
    )
    if not updated.rowcount:
        raise ActionError(f"no plan block {block_id}")
    return Result(ok=True, detail="pinned" if pinned else "unpinned")


# ── 6. edit an effort estimate ────────────────────────────────────────────


def set_estimate(conn: sqlite3.Connection, commitment_id: int, minutes: int) -> Result:
    """docs/06: "Edit an effort estimate."

    `estimate_source` becomes 'manual', which is the point of that column: a later change
    to the type-default table must not silently overwrite a number the owner chose.
    """
    if minutes < 0:
        raise ActionError("an estimate cannot be negative")
    _require_open(conn, commitment_id)
    conn.execute(
        "UPDATE commitment SET estimated_minutes = ?, estimate_source = 'manual' WHERE id = ?",
        (minutes, commitment_id),
    )
    return Result(ok=True, detail=f"{minutes}m")


# ── 7. adjust a weekly target ─────────────────────────────────────────────


def set_weekly_count(conn: sqlite3.Connection, target_id: int, weekly_count: int) -> Result:
    """docs/06: "Adjust a weekly target."."""
    if weekly_count < 0:
        raise ActionError("a weekly count cannot be negative")
    updated = conn.execute(
        "UPDATE target SET weekly_count = ? WHERE id = ?", (weekly_count, target_id)
    )
    if not updated.rowcount:
        raise ActionError(f"no target {target_id}")
    return Result(ok=True, detail=f"{weekly_count}/wk")


def mark_brief_opened(conn: sqlite3.Connection, brief_id: int) -> None:
    """docs/05 B7. Set once — the first open is the signal, not the last.

    "A brief nobody opens is the signal that matters most."
    """
    # `sent_at IS NOT NULL`: the pixel only means something for a brief that was
    # actually emailed. Without it, one hostile page with <img src="/b/1.gif">…/b/N.gif
    # marks the whole opened/not-opened history read, irreversibly.
    conn.execute(
        "UPDATE brief SET opened_at = ? WHERE id = ? AND opened_at IS NULL "
        "AND sent_at IS NOT NULL",
        (now_iso(), brief_id),
    )


# ── Phase 6: people ───────────────────────────────────────────────────────


def person_create(
    conn: sqlite3.Connection,
    *,
    name: str,
    role: str | None = None,
    org: str | None = None,
    tags: str = "",
    notes: str | None = None,
) -> int:
    """Manual profile. The one place a person can exist before any evidence does."""
    name = name.strip()
    if not name:
        raise ActionError("a person needs a name")
    tag_list = sorted({t.strip().lower() for t in tags.split(",") if t.strip()})
    try:
        cur = conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name, role, org, tags_json,"
            " notes, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (USER_ID, _kind_from_tags(tag_list) or "person", name, role or None,
             org or None, json.dumps(tag_list), notes or None, now_iso()),
        )
    except sqlite3.IntegrityError as exc:
        raise ActionError(f"'{name}' already exists") from exc
    return int(cur.lastrowid or 0)


def _kind_from_tags(tag_list: list[str]) -> str | None:
    """An `org`/`person` tag is the owner's classification, so it writes through to
    `entity.kind` rather than living only in the display heuristic. Safe since the
    resolver matches both kinds; `org` wins a tie the same way org_like reads it."""
    if "org" in tag_list:
        return "org"
    if "person" in tag_list:
        return "person"
    return None


def person_update(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    role: str | None = None,
    org: str | None = None,
    tags: str = "",
    notes: str | None = None,
) -> Result:
    tag_list = sorted({t.strip().lower() for t in tags.split(",") if t.strip()})
    kind = _kind_from_tags(tag_list)
    try:
        updated = conn.execute(
            "UPDATE entity SET role = ?, org = ?, tags_json = ?, notes = ?,"
            " kind = COALESCE(?, kind), updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (role or None, org or None, json.dumps(tag_list), notes or None,
             kind, now_iso(), entity_id, USER_ID),
        )
    except sqlite3.IntegrityError as exc:
        # UNIQUE(user_id, kind, canonical_name): the other kind already has this name.
        raise ActionError("another profile with this name already has that kind") from exc
    if not updated.rowcount:
        raise ActionError(f"no entity {entity_id}")
    return Result(ok=True, detail="updated")


# ── Phase 6: quick-add ────────────────────────────────────────────────────


def quick_add(
    conn: sqlite3.Connection,
    settings: Any,
    *,
    what: str,
    direction: str,
    counterparty: str | None = None,
    due_at: str | None = None,
    minutes: int | None = None,
) -> int:
    """A commitment typed by the owner, not extracted from evidence.

    `source_item_id` is NOT NULL by design, so the owner's own words become a manual
    source item first — provenance for a hand-entered claim is the claim itself, with a
    timestamp. `occurred_at` is now: unlike an old email, "by Friday" typed today means
    this Friday. The 0002 immutability trigger applies to it like any other item.
    """
    import uuid
    from hashlib import sha256

    from backglass.ledger import Ledger

    what = what.strip()
    if not what:
        raise ActionError("a commitment needs words")
    if direction not in ("i_owe", "owed_to_me"):
        raise ActionError(f"unknown direction {direction!r}")

    body = what if not counterparty else f"{what} — {counterparty}"
    cur = conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict, extraction_version)"
        " VALUES (?, 'manual', ?, ?, ?, ?, 'Manual entry', ?, ?, 'keep', 'manual')",
        # fetched_at is telemetry (UTC); occurred_at is the owner's claim and carries
        # their local date — typed tonight in Phoenix must not read as tomorrow.
        (USER_ID, uuid.uuid4().hex, now_iso(), timezones.local_now_iso(settings),
         (settings.owner_emails[0] if settings.owner_emails else settings.owner_name),
         body, sha256(body.encode()).hexdigest()),
    )
    source_item_id = int(cur.lastrowid or 0)

    ledger = Ledger(conn, settings)
    entity_id = ledger.resolve_entity(counterparty) if counterparty else None
    return ledger.insert_commitment(
        direction=direction,
        entity_id=entity_id,
        what=what,
        due_at=due_at or None,
        estimated_minutes=minutes,
        estimate_source="manual" if minutes else None,
        confidence=1.0,
        source_item_id=source_item_id,
        # The owner's own sentence is the evidence for a hand-entered claim. Citing it
        # keeps the read path uniform — every commitment on every surface has a quote,
        # and a manual row does not render as the one with nothing behind it.
        evidence=body,
        evidence_kind="manual",
    )
