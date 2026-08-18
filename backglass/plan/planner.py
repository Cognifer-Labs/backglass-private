"""The day planner. docs/04 §1.

    "Every morning the planner emits a **proposed day** ... The word 'proposed' is doing
    real work. The planner never writes to the real calendar without confirmation. It
    suggests; the owner accepts, edits, or ignores. A planner that silently rearranges
    your calendar gets turned off in week one."

Requirements implemented here:

  P1  capacity computed before any selection; never select past it
  P2  overflow is stated explicitly, never silently truncated
  P3  under 60 minutes of capacity, propose nothing and say the day is booked
  P4  minimum block 25 minutes; smaller items batch into one "small items" block
  P5  never schedule across a fixed event, never overlap
  P6  at least one contiguous >= 90 minute protected block per weekday when capacity allows
  P7  the protected block goes in the peak window
  P8  small items and admin never go in the protected block
  P9  when no 90-minute gap exists, say so plainly
  P10 rollover items sit at the top of the next day's proposal
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan import capacity as capacity_mod
from backglass.plan import estimates, timezones
from backglass.plan import preferences as preferences_mod
from backglass.plan.capacity import Capacity, Slot

#: docs/04 §1.5. "Priority is derived, not entered."
#: `overdue > due today > advances an at-risk goal > due this week > everything else`
PRIORITY_OVERDUE = 0
PRIORITY_DUE_TODAY = 1
PRIORITY_AT_RISK_GOAL = 2
PRIORITY_DUE_THIS_WEEK = 3
PRIORITY_REST = 4


@dataclass
class Candidate:
    commitment_id: int
    what: str
    due_at: str | None
    minutes: int
    direction: str
    goal_id: int | None
    rollover_count: int
    priority: int
    blocked_on_others: bool
    age_days: int

    @property
    def small(self) -> bool:
        return False  # decided against settings in `select`, not here


@dataclass
class Proposal:
    day: date
    tz: str
    capacity: Capacity
    blocks: list[dict[str, Any]] = field(default_factory=list)
    overflow: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    protected_placed: bool = False
    #: Hash of what this plan was built from (inputs_fingerprint, 0028). Stamped by
    #: propose(), persisted beside the plan, compared by the replanner.
    fingerprint: str = ""
    #: P3's "list only what is due", as a list rather than as an adjective on a pile of
    #: forty-eight. On a day the planner declines to plan, this is the subset a reader
    #: must not miss: overdue, or due today. It is a view of `overflow`, never a second
    #: source of truth — every item here is also there, so a renderer that ignores it
    #: still shows everything.
    due_now: list[Candidate] = field(default_factory=list)

    @property
    def planned_minutes(self) -> int:
        return sum(
            int(b["minutes"])
            for b in self.blocks
            if b["kind"] in ("work", "protected", "small")
        )


def candidates(
    conn: sqlite3.Connection, settings: Settings, day: date, at_risk_goals: set[int]
) -> list[Candidate]:
    """Open commitments the planner may schedule, with derived priority.

    Only `i_owe`. An `owed_to_me` commitment is someone else's work; scheduling time for
    it would be scheduling time to wait.
    """
    rows = conn.execute(
        "SELECT c.id, c.what, c.due_at, c.estimated_minutes, c.direction, c.goal_id, "
        "       c.rollover_count, s.occurred_at "
        "FROM commitment c JOIN source_item s ON s.id = c.source_item_id "
        "WHERE c.user_id = ? AND c.status = 'open' AND c.confidence >= ? "
        "  AND c.direction = 'i_owe'",
        (USER_ID, settings.confidence_threshold),
    ).fetchall()

    week_end = day + timedelta(days=(6 - day.weekday()))
    out: list[Candidate] = []
    for row in rows:
        due = date.fromisoformat(str(row["due_at"])[:10]) if row["due_at"] else None
        if due is not None and due < day:
            priority = PRIORITY_OVERDUE
        elif due == day:
            priority = PRIORITY_DUE_TODAY
        elif row["goal_id"] in at_risk_goals:
            priority = PRIORITY_AT_RISK_GOAL
        elif due is not None and due <= week_end:
            priority = PRIORITY_DUE_THIS_WEEK
        else:
            priority = PRIORITY_REST

        occurred = date.fromisoformat(str(row["occurred_at"])[:10])
        out.append(
            Candidate(
                commitment_id=int(row["id"]),
                what=str(row["what"]),
                due_at=str(row["due_at"]) if row["due_at"] else None,
                minutes=int(row["estimated_minutes"] or 0),
                direction=str(row["direction"]),
                goal_id=row["goal_id"],
                rollover_count=int(row["rollover_count"] or 0),
                priority=priority,
                # docs/04 §1.5 rule 3: "Blocked-on-others items get scheduled early in the
                # day, so the ask goes out with a full working day left for a response."
                blocked_on_others=_is_an_ask(str(row["what"])),
                age_days=(day - occurred).days,
            )
        )
    return out


def _is_an_ask(what: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(ask|chase|follow up|request|ping|email|call|send to|check with)\b", what, re.I
        )
    )


def order(
    items: list[Candidate], prefs: preferences_mod.Preferences | None = None
) -> list[Candidate]:
    """docs/04 §1.5, everything after rule 1 (fixed events, which the planner cannot move).

    Rollover first (P10: "Rollover items appear at the top of the next day's proposal,
    above newly selected work"), then asks-of-others, then derived priority, then the
    owner's stated lanes (plan/preferences.py), then age. The lane sits INSIDE the
    priority band on purpose: "school beats social" is a statement about what matters,
    not a licence to plan next week's homework over today's overdue favour.
    """
    lane_rank = prefs.rank if prefs is not None else (lambda _what: 0)
    return sorted(
        items,
        key=lambda c: (
            0 if c.rollover_count else 1,
            0 if c.blocked_on_others else 1,
            c.priority,
            lane_rank(c.what),
            -c.age_days,
        ),
    )


def select(
    items: list[Candidate],
    cap: Capacity,
    settings: Settings,
    prefs: preferences_mod.Preferences | None = None,
) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    """P1 and P2. Returns (scheduled, small, overflow).

    "Never select past capacity", and "if selected work exceeds capacity, drop
    lowest-priority items and say so explicitly". Overflow is returned rather than
    discarded so the caller can count it into the brief.
    """
    remaining = cap.capacity_minutes
    scheduled: list[Candidate] = []
    small: list[Candidate] = []
    overflow: list[Candidate] = []

    for item in order(items, prefs):
        minutes = max(1, item.minutes)
        if minutes > remaining:
            overflow.append(item)
            continue
        # P4: "Blocks have a minimum size of 25 minutes. Anything smaller is batched into
        # a single 'small items' block."
        if minutes < settings.min_block_minutes:
            small.append(item)
        else:
            scheduled.append(item)
        remaining -= minutes
    return scheduled, small, overflow


def _peak_slot(cap: Capacity, settings: Settings, day: date) -> Slot | None:
    """P7. The longest slot overlapping the peak window, if it is long enough for P6."""
    peak_start, peak_end = timezones.parse_window(settings.peak_window)
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(cap.tz)
    peak_from = datetime.combine(day, peak_start, tzinfo=zone)
    peak_to = datetime.combine(day, peak_end, tzinfo=zone)

    best: Slot | None = None
    for slot in cap.slots:
        start = max(slot.starts_at, peak_from)
        end = min(slot.ends_at, peak_to)
        overlap = int((end - start).total_seconds() // 60)
        if overlap >= settings.protected_block_minutes and (
            best is None or overlap > best.minutes
        ):
            best = Slot(start, start + timedelta(minutes=settings.protected_block_minutes))
    if best is not None:
        return best
    # P6 says "when capacity allows" and P7 places it in the peak window. If the peak
    # window is fragmented but a long gap exists elsewhere, protecting that is better than
    # protecting nothing — and it is still reported as protected, so the owner can see
    # where it landed.
    longest = cap.longest_slot()
    if longest and longest.minutes >= settings.protected_block_minutes:
        return Slot(
            longest.starts_at,
            longest.starts_at + timedelta(minutes=settings.protected_block_minutes),
        )
    return None


def inputs_fingerprint(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    at_risk_goals: set[int] | None = None,
) -> str:
    """A deterministic hash of everything a plan for `day` is built from.

    Deliberately CLOCK-FREE: the `now` clamp is how a plan describes the hours that
    are left, but two plans built at 05:45 and 14:00 from the same world must hash
    the same, or every afternoon sync would report drift that is only the time
    passing. What goes in: the candidate pool (ids, texts, minutes, dues, priority,
    rollovers), the day's fixed events, the working window, the timezone, and the
    owner's stated lanes. What a sync changes, this changes; what the clock changes,
    it does not.
    """
    import hashlib
    import json

    pool = candidates(conn, settings, day, at_risk_goals or set())
    tz = timezones.active_tz(settings, day)
    events = capacity_mod.day_events(conn, settings, day)
    prefs = preferences_mod.load(conn)
    window = timezones.window_on(settings, day)
    payload = {
        "candidates": sorted(
            (c.commitment_id, c.what, c.minutes, c.due_at or "", c.priority,
             c.rollover_count, c.blocked_on_others)
            for c in pool
        ),
        "events": sorted(
            (e.starts_at.isoformat(), e.ends_at.isoformat(), e.title, e.kind)
            for e in events
        ),
        "window": [t.isoformat() for t in window] if window else [],
        "tz": tz,
        "lanes": [[lane.name, list(lane.keywords)] for lane in prefs.lanes],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def propose(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    *,
    at_risk_goals: set[int] | None = None,
    events: list[capacity_mod.FixedEvent] | None = None,
    now: datetime | None = None,
) -> Proposal:
    """Build the proposed day. Writes nothing — `persist` does that.

    `now` is the wall clock, and it only ever matters when it falls on `day`: it clamps
    capacity to the hours that are left. launchd defers a missed calendar interval to the
    next wake, so on a machine that sleeps through 05:45 this runs in the evening, and
    without the clamp it answered by packing a morning that had already gone — a plan
    with breakfast at 07:30, written at 17:22. Callers that know the clock (the CLI, the
    catch-up net) pass it; tests and what-ifs leave it None and get the whole window.
    """
    estimates.backfill(conn, settings)
    cap = capacity_mod.compute(
        conn,
        settings,
        day,
        events=events,
        not_before=now if (now is not None and now.date() == day) else None,
    )
    proposal = Proposal(day=day, tz=cap.tz, capacity=cap)
    proposal.fingerprint = inputs_fingerprint(conn, settings, day, at_risk_goals)

    change = timezones.changed_on(settings, day)
    if change:
        # P15. The brief leads with this; the plan records it so the brief can.
        proposal.notes.append(f"Timezone changed {change[0]} → {change[1]}.")

    # Fixed events sit where they sit (docs/04 §1.5 rule 1). The whole day's picture,
    # not the window-clipped list capacity computed with: a 7:15pm dinner and a 7:30am
    # breakfast belong on the plan even though neither spends working capacity. An
    # explicit `events` list stays the whole picture, same contract as `compute`.
    whole_day = (
        list(events) if events is not None else capacity_mod.day_events(conn, settings, day)
    )
    for event in whole_day:
        proposal.blocks.append(
            {
                "starts_at": event.starts_at.isoformat(),
                "ends_at": event.ends_at.isoformat(),
                "kind": event.kind,
                "title": event.title,
                "minutes": event.minutes,
                "commitment_id": None,
                "goal_id": None,
            }
        )

    if not cap.plannable:
        # P3. "Say the day is fully booked and list only what is due."
        proposal.overflow = order(
            candidates(conn, settings, day, at_risk_goals or set()), preferences_mod.load(conn)
        )
        proposal.due_now = [c for c in proposal.overflow if c.priority <= PRIORITY_DUE_TODAY]
        if cap.window_closed:
            # The day was workable and is now over. Distinct from both neighbours: there
            # is nothing to decline and nothing wrong with the calendar, the hours are
            # simply gone. This is the ordinary case for a run launchd deferred to the
            # evening, so it must not read as a fault.
            # Read back from the day itself, not from `working_window`: a Saturday runs
            # on `weekend_window`, and naming the weekday's hour on a weekend would be a
            # precise-looking sentence that is false.
            closed_at = timezones.window_on(settings, day)[1].strftime("%H:%M")
            proposal.notes.append(
                f"The working window closed at {closed_at} — nothing left to plan today."
            )
        elif cap.no_window:
            # Not booked — not a working day. Saying "fully booked" here sends the owner
            # looking for meetings that are not there, and it is what hid a move-in.
            proposal.notes.append(
                f"{day.strftime('%A')} is not a working day — no plan proposed."
            )
        else:
            proposal.notes.append(
                f"Fully booked — {cap.capacity_minutes}m of capacity, under the "
                f"{settings.min_capacity_minutes}m floor. No plan proposed."
            )
        if proposal.due_now:
            # The whole point of P3's "list only what is due": one obligation due today
            # must not arrive as item thirty-one of a list nobody reads to the end.
            proposal.notes.append(
                f"{len(proposal.due_now)} due or overdue: "
                + "; ".join(c.what for c in proposal.due_now[:3])
                + ("…" if len(proposal.due_now) > 3 else "")
            )
        return proposal

    pool = candidates(conn, settings, day, at_risk_goals or set())
    prefs = preferences_mod.load(conn)
    for warning in prefs.warnings:
        # A preference silently ignored is worse than none — the owner believes it is
        # being applied. Same register as the fragmented-calendar sentence.
        proposal.notes.append(warning)
    scheduled, small, overflow = select(pool, cap, settings, prefs)
    proposal.overflow = overflow

    protected = _peak_slot(cap, settings, day)
    if protected is None:
        # P9. "That sentence is the product. Seeing it three days running is what prompts
        # someone to change their meeting habits."
        proposal.notes.append("No deep work block available today, calendar is fragmented.")

    cursor_by_slot = {id(slot): slot.starts_at for slot in cap.slots}
    if protected is not None and scheduled:
        # P8. Small items and admin never go in the protected block — the first *real*
        # piece of work takes it, and the small-items batch is placed elsewhere.
        head = scheduled.pop(0)
        proposal.blocks.append(
            {
                "starts_at": protected.starts_at.isoformat(),
                "ends_at": protected.ends_at.isoformat(),
                "kind": "protected",
                "title": head.what,
                "minutes": protected.minutes,
                "commitment_id": head.commitment_id,
                "goal_id": head.goal_id,
            }
        )
        proposal.protected_placed = True
        for slot in cap.slots:
            if slot.starts_at <= protected.starts_at < slot.ends_at:
                cursor_by_slot[id(slot)] = protected.ends_at

    def place(
        title: str, minutes: int, kind: str, commitment_id: int | None, goal_id: int | None
    ) -> bool:
        """P5. Work only ever lands inside a free slot, so it cannot cross a fixed event."""
        for slot in cap.slots:
            cursor = cursor_by_slot[id(slot)]
            if int((slot.ends_at - cursor).total_seconds() // 60) >= minutes:
                end = cursor + timedelta(minutes=minutes)
                proposal.blocks.append(
                    {
                        "starts_at": cursor.isoformat(),
                        "ends_at": end.isoformat(),
                        "kind": kind,
                        "title": title,
                        "minutes": minutes,
                        "commitment_id": commitment_id,
                        "goal_id": goal_id,
                    }
                )
                cursor_by_slot[id(slot)] = end
                return True
        return False

    for item in scheduled:
        if not place(item.what, max(1, item.minutes), "work", item.commitment_id, item.goal_id):
            proposal.overflow.append(item)

    if small:
        total = max(settings.min_block_minutes, sum(max(1, s.minutes) for s in small))
        title = f"Small items ({len(small)})"
        if not place(title, total, "small", None, None):
            proposal.overflow.extend(small)

    if proposal.overflow:
        # P2. Never silently truncate.
        proposal.notes.append(f"{len(proposal.overflow)} item(s) did not fit.")

    proposal.blocks.sort(key=lambda b: str(b["starts_at"]))
    return proposal


def current_plan_id(conn: sqlite3.Connection, day: date) -> int | None:
    """The live (non-superseded) `day_plan` for `day`, or None if the day has none.

    Same `status != 'superseded'` predicate the brief reads a day's plan with
    (`brief/daily.py`), so "already planned" means the same thing in both places.
    """
    row = conn.execute(
        "SELECT id FROM day_plan WHERE user_id = ? AND local_date = ? "
        "AND status != 'superseded' ORDER BY id DESC LIMIT 1",
        (USER_ID, day.isoformat()),
    ).fetchone()
    return int(row["id"]) if row else None


def persist(conn: sqlite3.Connection, settings: Settings, proposal: Proposal) -> int:
    """Write the proposal as a new `day_plan`, superseding any prior one for that date.

    docs/04 §3: "`day_plan` is versioned by `status` rather than overwritten. Regenerating
    a plan supersedes the old one and keeps it. You will want the history the first time
    you ask whether the planner is actually any good."
    """
    conn.execute(
        "UPDATE day_plan SET status = 'superseded' "
        "WHERE user_id = ? AND local_date = ? AND status != 'superseded'",
        (USER_ID, proposal.day.isoformat()),
    )
    conn.execute(
        "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes, planned_minutes, "
        " overflow_count, generated_at, status, inputs_fingerprint)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)",
        (
            USER_ID,
            proposal.day.isoformat(),
            proposal.tz,
            proposal.capacity.capacity_minutes,
            proposal.planned_minutes,
            len(proposal.overflow),
            now_iso(),
            proposal.fingerprint or None,
        ),
    )
    plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    for block in proposal.blocks:
        rollover = 0
        if block["commitment_id"] is not None:
            row = conn.execute(
                "SELECT rollover_count FROM commitment WHERE id = ?", (block["commitment_id"],)
            ).fetchone()
            rollover = int(row["rollover_count"] or 0) if row else 0
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, commitment_id, "
            " goal_id, title, pinned, outcome, rollover_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?)",
            (
                plan_id,
                block["starts_at"],
                block["ends_at"],
                block["kind"],
                block["commitment_id"],
                block["goal_id"],
                block["title"],
                rollover,
            ),
        )
    return plan_id
