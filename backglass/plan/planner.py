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
  P18 a routine the day left no room for is stated, never silently dropped (§1.9)
  P20 a day whose plan holds no coursework gets one study block, and it never rolls
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backglass import staleness
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan import capacity as capacity_mod
from backglass.plan import estimates, timezones
from backglass.plan import preferences as preferences_mod
from backglass.plan import runway as runway_mod
from backglass.plan.capacity import Capacity, Slot

#: docs/04 §1.5. "Priority is derived, not entered."
#: `overdue > due today > advances an at-risk goal > due this week > everything else`
PRIORITY_OVERDUE = 0
PRIORITY_DUE_TODAY = 1
PRIORITY_AT_RISK_GOAL = 2
#: Work the fortnight's allocation says has to start today — `plan/runway.py`. Added
#: 2026-08-27, and the first band derived from something other than this one day.
#:
#: It sits here rather than at either end for a reason on each side. Above "due this
#: week", because the runway has already weighed every deadline in the horizon against
#: every day's real capacity, and a thing it says to start today is a thing that does not
#: finish otherwise — while "due this week" is a calendar fact that knows nothing about
#: whether the week has room. Below "due today" and "at risk", because those are
#: statements the runway does not make: one is a promise coming due now, the other is the
#: goal engine's own alarm, and a forecast must not outrank either.
PRIORITY_ALLOCATED_TODAY = 3
PRIORITY_DUE_THIS_WEEK = 4
PRIORITY_REST = 5


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
    #: Minutes already worked on this obligation, from blocks marked done on earlier days.
    #: Zero for everything that has never been scheduled, which is almost everything.
    done_minutes: int = 0
    #: What today's plan is giving it. Set by `select`, read by `propose`; zero until then.
    planned_minutes: int = 0
    #: Whether the work can be done across sittings. True only for an estimate
    #: `coursework` read off the assignment (`estimate_source = 'analyzed'`), which is a
    #: deliverable measured in pages, chapters or runtime and divisible by construction.
    #: A three-hour move-in is not, and nothing here may cut one in half.
    divisible: bool = False
    #: `assignment.lock_at`, where it is earlier than the due date — the day the door
    #: shuts rather than the day the work is wanted. Migration 0036. `priority` is ranked
    #: on it; `due_at` above is left as the ledger holds it, so a surface that shows the
    #: earlier date can also say where it came from instead of appearing to contradict
    #: Canvas. NULL for everything with no assignment behind it, which is most things.
    closes_at: str | None = None

    @property
    def small(self) -> bool:
        return False  # decided against settings in `select`, not here

    @property
    def remaining(self) -> int:
        """What is left of the work, not what it was when it started."""
        return max(1, self.minutes - self.done_minutes)

    def sitting(self, settings: Settings) -> int:
        """How much of it to put in one block.

        `coursework` reads real numbers off the assignment now: four exams at two hours,
        five CIS 236 milestones between 138 and 344 minutes. Handed to the planner whole,
        every one of them is *unschedulable* — `select` drops any candidate larger than
        the capacity left in the day and `place` needs a contiguous slot that long — so
        honest estimates would have made the biggest work vanish from every plan, which
        is strictly worse than the flat thirty minutes they replaced.

        So divisible work is capped at a sitting, the obligation stays open, and the rest
        comes back tomorrow. `done_minutes` is what stops that being an endless loop: the
        work shrinks as it is done, and `actions.set_block_outcome` resolves the
        commitment once the sittings add up.

        Divisible, and not merely long. The discriminator is evidence rather than size:
        an `analyzed` estimate is one `coursework` read off the assignment — pages,
        chapters, a runtime — and a deliverable measured that way is done across sittings
        by construction. "Move-in: Willow Hall 502" is three hours of one thing, carries
        a number a person chose, and is planned whole or not at all. Nobody writes a
        344-minute report in one sitting either; docs/04 protects 90 minutes because
        that is what a sitting is.
        """
        if not self.divisible:
            return self.remaining
        return min(self.remaining, max(1, settings.max_block_minutes))

    @property
    def is_coursework(self) -> bool:
        """Homework, for the reservation in `select`.

        The same flag as `divisible`, named for the other thing it means. An `analyzed`
        estimate is one `coursework.py` read off a Canvas assignment — its pages, its
        chapters, its runtime — so "this number came from an assignment" and "this is
        homework" are the same fact about the row. Two names because the two uses are
        unrelated: one decides whether work can be split across sittings, the other
        decides whose claim on the day comes first, and a later change to either must
        not silently move the other.
        """
        return self.divisible


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
    #: Whether this plan is the relief pass — the day extended into the evening and the
    #: sacrificial routines yielded. Normally False, and a reader that wants to know
    #: *what* yielded reads the notes, which name each one.
    relieved: bool = False

    @property
    def planned_minutes(self) -> int:
        return sum(
            int(b["minutes"])
            for b in self.blocks
            if b["kind"] in ("work", "protected", "small", "study", "coding")
        )


def candidates(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    at_risk_goals: set[int],
    stale: set[int] | None = None,
    allocated_today: set[int] | None = None,
    not_yet: set[int] | None = None,
    on_calendar: set[int] | None = None,
) -> list[Candidate]:
    """Open commitments the planner may schedule, with derived priority.

    Only `i_owe`. An `owed_to_me` commitment is someone else's work; scheduling time for
    it would be scheduling time to wait.

    Stale rows are out (`staleness.stale_ids`). `PRIORITY_OVERDUE` ranks the most lapsed
    item first and never expires it, so without this gate the oldest dead obligation on
    the board outranks everything real, permanently: on 2026-08-18 the day was given four
    blocks of college-admissions work whose deadlines passed in May, for schools the
    owner does not attend. The same predicate is already asking the owner "still real?",
    and half an hour scheduled for it is the system answering its own question with yes.
    Not silently dropped — a gated row is a question, and answering `STALE_KEEP` returns
    it to the very next plan.

    `stale` is passed in by `propose`, which also counts it for the held-back note: the
    set the note describes and the set the gate applies are then the same object, and the
    windowed evidence query runs once per proposal rather than twice.

    `allocated_today` is `plan/runway.py`'s answer for this day, passed in the same way
    and for the same reason. It is the only input here that knows about any day but this
    one, and it is what stops an assignment due in three weeks being invisible for two of
    them and then arriving with nowhere left to go.

    `not_yet` and `on_calendar` are out-parameters, filled with the commitments this
    refused and why: locked until a later date (migration 0036), and already sitting on
    the calendar as an event (migration 0037). Sets the caller passes in rather than
    second and third return values, for the same reason `stale` is passed in: `propose`
    needs the counts for its notes, and the set a note describes has to be the same object
    the gate applied.

    Three different reasons a commitment is not on today's board, and the point of
    separating them is that they mean opposite things. Stale is a question. Not-yet is a
    door that has not opened. On-calendar is the only one that is good news.
    """
    if stale is None:
        stale = staleness.stale_ids(conn, day)
    rows = conn.execute(
        "SELECT c.id, c.what, c.due_at, c.estimated_minutes, c.direction, c.goal_id, "
        "       c.rollover_count, c.estimate_source, s.occurred_at, "
        # The assignment behind the commitment, where there is one, for the two dates
        # migration 0036 added. LEFT JOIN and only through `source_item`: a commitment
        # with no assignment gets NULLs and behaves exactly as it did before.
        "       a.unlock_at, a.lock_at, c.scheduled_source_item_id, "
        # Sittings already spent on it. A multi-session assignment that keeps its full
        # estimate every morning would be scheduled forever; this is what makes the
        # remainder shrink.
        "       COALESCE(("
        "         SELECT SUM((julianday(b.ends_at) - julianday(b.starts_at)) * 1440) "
        "         FROM plan_block b WHERE b.commitment_id = c.id AND b.outcome = 'done'"
        "       ), 0) AS done_minutes "
        "FROM commitment c JOIN source_item s ON s.id = c.source_item_id "
        "     LEFT JOIN assignment a ON a.source_item_id = c.source_item_id "
        "WHERE c.user_id = ? AND c.status = 'open' AND c.confidence >= ? "
        "  AND c.direction = 'i_owe'",
        (USER_ID, settings.confidence_threshold),
    ).fetchall()

    week_end = day + timedelta(days=(6 - day.weekday()))
    zone = timezones.active_tz(settings, day)
    out: list[Candidate] = []
    for row in rows:
        if int(row["id"]) in stale:
            continue
        if row["scheduled_source_item_id"] is not None:
            # It is already on the calendar, and `capacity` has already taken the day's
            # minutes for it. Offering it to `select` as well is how commitment 588's
            # ninety minutes got charged twice for one Sep 2 pod session — once as the
            # fixed event and once as work to fit around it (migration 0037).
            if on_calendar is not None:
                on_calendar.add(int(row["id"]))
            continue

        unlock = _day_of(row["unlock_at"], zone)
        if unlock is not None and unlock > day:
            # "Not yet" is a third answer, and it is the one the ledger could not give.
            # CHM 113's Act 2 signup was locked until Sep 3 and due Sep 10: without this
            # it sorted into PRIORITY_REST and was counted, every single morning, in the
            # same "did not fit" number as work the day genuinely had no room for. Those
            # are different facts and a plan that renders them with one word is lying
            # about one of them. `not_yet` collects them for the note in `propose`.
            if not_yet is not None:
                not_yet.add(int(row["id"]))
            continue

        stated = _day_of(row["due_at"], zone)
        closes = _day_of(row["lock_at"], zone)
        # A window that shuts before the due date IS the deadline. BIO 181's Act I pod
        # signup closes Sep 4 for a workbook due Sep 7 — ranking on Sep 7 schedules it
        # three days after the door it has to go through.
        #
        # Ranked on, not displayed as. `Candidate.due_at` stays the date the ledger
        # holds, because a block that says "due Sep 4" where Canvas says Sep 10 is an
        # unsourced claim (rule 1) and the owner has no way to see where it came from.
        # The earlier date rides along in `closes_at` for a surface that wants to say
        # *why*, and this function stays the only place that decides priority.
        due = stated
        if closes is not None and (due is None or closes < due):
            due = closes

        if due is not None and due < day:
            priority = PRIORITY_OVERDUE
        elif due == day:
            priority = PRIORITY_DUE_TODAY
        elif row["goal_id"] in at_risk_goals:
            priority = PRIORITY_AT_RISK_GOAL
        elif int(row["id"]) in (allocated_today or set()):
            # The fortnight's answer, not this morning's. `runway.allocate` walked every
            # day between here and each deadline and put this one's next sitting today;
            # without this band it would sort into `PRIORITY_REST` with three hundred
            # other things and lose the day to whichever of them happened to be oldest.
            priority = PRIORITY_ALLOCATED_TODAY
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
                done_minutes=int(row["done_minutes"] or 0),
                divisible=str(row["estimate_source"] or "") == "analyzed",
                closes_at=(
                    str(row["lock_at"])
                    if closes is not None and (stated is None or closes < stated)
                    else None
                ),
            )
        )
    return out


def _day_of(value: object, zone: str) -> date | None:
    """The date this timestamp falls on **in the owner's zone**, or None.

    Not the first ten characters, and the difference is a whole day. Canvas returns
    `unlock_at` and `lock_at` in UTC: "Available Sep 3 at 12am - Sep 15 at 11:59pm"
    Phoenix comes back as `2026-09-03T07:00:00Z` and `2026-09-16T06:59:59Z`. Slicing
    those gives the 3rd — right — and the **16th** — a day of deadline that does not
    exist. The owner moves between UTC-7 and UTC+5:30, so this is not a Phoenix-shaped
    problem that could be hardcoded away either.

    A bare `2026-09-03` from the ICS feed has no zone to convert and is already a date;
    it is taken as written.
    """
    if not value:
        return None
    text = str(value)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if stamp.tzinfo is None:
        return stamp.date()
    return stamp.astimezone(ZoneInfo(zone)).date()


def _block_title(item: Candidate, minutes: int) -> str:
    """What the block is called, and whether it admits to being part of something.

    A block reading "T - Final Analysis" for 90 minutes of a 344-minute deliverable would
    be a quiet lie about what finishing it means, and the owner marks these done by
    reading them. Shared by the protected block and the ordinary work blocks, because the
    protected slot takes the first real piece of work and that is exactly the piece most
    likely to be too big for one sitting.
    """
    if minutes >= item.remaining:
        return item.what
    return f"{item.what} ({minutes}m of {item.remaining}m left)"


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

    Derived priority first, then rollover, then asks-of-others, then the owner's stated
    lanes (plan/preferences.py), then age.

    **Rollover sits INSIDE the priority band, since 2026-08-24**, for the same reason the
    lane rank already did. P10 says "rollover items appear at the top of the next day's
    proposal, above newly selected work", and that is a statement about the things you
    would otherwise do *today* — the thing you failed to get to yesterday leads them. As
    the first key it meant something else: anything that has ever rolled beat everything
    that has not, whatever either was due, permanently. Measured on the owner's real
    2026-08-26 plan, it handed the day's first ninety minutes to a CIS 236 RFP due **13
    November** while seven assignments due **28 August** waited behind it. A key that
    never expires is not a tiebreak, and P10 is not a licence to lead the day with next
    semester.
    """
    lane_rank = prefs.rank if prefs is not None else (lambda _what: 0)
    return sorted(
        items,
        key=lambda c: (
            c.priority,
            0 if c.rollover_count else 1,
            0 if c.blocked_on_others else 1,
            lane_rank(c.what),
            -c.age_days,
        ),
    )


def select(
    items: list[Candidate],
    cap: Capacity,
    settings: Settings,
    prefs: preferences_mod.Preferences | None = None,
    *,
    protected_minutes: int = 0,
) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    """P1 and P2. Returns (scheduled, small, overflow).

    "Never select past capacity", and "if selected work exceeds capacity, drop
    lowest-priority items and say so explicitly". Overflow is returned rather than
    discarded so the caller can count it into the brief.

    Two things this does beyond spending a budget, both added 2026-08-24 after reading the
    owner's live 2026-08-26 plan rather than a fixture:

    **It never selects work it cannot place.** `capacity_minutes` is a sum and placement
    needs a contiguous run. That day had 215 minutes of capacity in six fragments whose
    largest was 60, so the old `select` happily spent 90 of them on one sitting, `place`
    found nowhere to put it, and the minutes were gone from the budget anyway — 175 of 183
    candidates overflowed on a day with three and a half free hours. Divisible work is
    clamped to the largest free slot; indivisible work that is bigger than every hole is
    overflow immediately rather than after eating the day.

    **Coursework gets first claim on `homework_target_minutes`.** The owner's ruling, this
    session: a weekday should hold about two hours of homework. It is a reservation rather
    than a second pass — other lanes may not spend the reserved slice while coursework is
    still waiting for it — and it lapses to nothing when there is no coursework to want it,
    so a day with nothing due is never held artificially empty. `analyzed` is the
    discriminator, same as `divisible`: it is an estimate `coursework.py` read off a real
    assignment, which is exactly what "homework" means here.
    """
    # P1, made true. The protected block is a *fixed* run — 90 minutes whatever the work
    # inside it needs — and `propose` fills it with the first scheduled item. Before
    # 2026-08-24 nobody charged the difference: on the owner's live plan for that day a
    # 45-minute obligation was placed in a 90-minute protected slot, so the day claimed
    # 248 minutes against a stated capacity of 205, and every later sizing computed from
    # "capacity left" started from a negative number — which is why the Coding block
    # silently did not exist. Charged up front, the head item then rides in that slot for
    # free, and `planned_minutes <= capacity_minutes` holds again.
    remaining = cap.capacity_minutes - protected_minutes
    head_is_free = protected_minutes > 0
    largest = cap.largest_slot
    scheduled: list[Candidate] = []
    small: list[Candidate] = []
    overflow: list[Candidate] = []

    ordered = order(items, prefs)
    # What coursework could actually use today, so the reservation cannot exceed the
    # demand for it. Without this a day with one 20-minute quiz would fence off two hours
    # against everything else and then not spend them.
    wanted = sum(min(c.sitting(settings), largest) for c in ordered if c.is_coursework)
    reserved = min(settings.homework_target_minutes, wanted, max(0, remaining))

    for item in ordered:
        # A sitting of it, which for everything indivisible is the whole thing — see
        # `Candidate.sitting`. Without the cap a 344-minute milestone is overflow on
        # every day of its life, and honest estimates would make the biggest work
        # invisible rather than schedulable.
        minutes = item.sitting(settings)
        if item.divisible:
            # Not clamped to the largest hole any more: divisible work is placed in
            # pieces (`place_split`), so a 90-minute sitting on a day of 60- and
            # 30-minute gaps is two sittings rather than a truncated one. It is still
            # bounded by `remaining` below, which is the day's free minutes in total.
            pass
        elif minutes > largest:
            # Indivisible and bigger than every hole in the day. It is overflow whatever
            # the budget says, and saying so here keeps its minutes available for work
            # that can actually be placed.
            overflow.append(item)
            continue
        # Coursework draws on the whole remaining budget; everything else must leave the
        # reserved slice alone.
        budget = remaining if item.is_coursework else remaining - reserved
        if minutes > budget:
            overflow.append(item)
            continue
        if item.is_coursework:
            reserved = max(0, reserved - minutes)
        item.planned_minutes = minutes
        # P4: "Blocks have a minimum size of 25 minutes. Anything smaller is batched into
        # a single 'small items' block."
        if minutes < settings.min_block_minutes:
            small.append(item)
            remaining -= minutes
        else:
            scheduled.append(item)
            if head_is_free:
                # The first real piece of work is the one `propose` moves into the
                # protected slot, whose minutes were already taken off the budget above.
                head_is_free = False
            else:
                remaining -= minutes
    return scheduled, small, overflow


def _peak_slot(cap: Capacity, settings: Settings, day: date) -> Slot | None:
    """P7. The longest slot overlapping the peak window, if it is long enough for P6.

    None when the feature is switched off. Found 2026-08-24: at
    `protected_block_minutes = 0` this returned a **zero-width** slot, `propose` popped
    the first real piece of work into it, and the day shipped a block reading
    "quick admin (0m of 25m left)" — the work neither scheduled nor overflowed, just
    quietly gone. Every other knob in this file turns its feature off at zero; this one
    turned it into a hole.
    """
    if settings.protected_block_minutes <= 0:
        return None
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


def tomorrow_preview(conn: sqlite3.Connection, day: date) -> list[str]:
    """What the day AFTER holds, as lines a note or a banner can carry.

    The morning plan answers "what do I do today" and stayed silent about the 8am exam
    tomorrow — which is decided today, by leaving room to prepare. Confirmed
    engagements only (a proposal is not yet an obligation) and open commitments due
    tomorrow; both compared on substr(…, 1, 10) for the usual mixed-shape reason.
    """
    tomorrow = (day + timedelta(days=1)).isoformat()
    lines: list[str] = []
    for r in conn.execute(
        "SELECT what, starts_at FROM engagement WHERE user_id = ?"
        " AND status = 'confirmed' AND starts_at IS NOT NULL"
        " AND substr(starts_at, 1, 10) = ?"
        " ORDER BY substr(starts_at, 1, 10), starts_at, id",
        (USER_ID, tomorrow),
    ):
        clock = str(r["starts_at"])[11:16]
        lines.append(f"{r['what']} at {clock}" if clock else str(r["what"]))
    for r in conn.execute(
        "SELECT what FROM commitment WHERE user_id = ? AND status = 'open'"
        " AND due_at IS NOT NULL AND substr(due_at, 1, 10) = ? ORDER BY id",
        (USER_ID, tomorrow),
    ):
        lines.append(f'"{r["what"]}" due')
    return lines


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

    **The fortnight is deliberately not in it.** `propose` consults `plan/runway.py`,
    which reads the calendar of the next fourteen days, so a class cancelled next Tuesday
    can move which obligation the runway puts on today — and this hash will not notice,
    so the replanner will not knock. That is the intended trade rather than an oversight:
    hashing fourteen days of calendar would make almost every sync report drift, since
    something in a fortnight changes nearly every time, and a knock that fires daily is a
    knock the owner turns off. `candidates` is called here *without* the allocation for
    the same reason — the priority band the runway assigns is downstream of this hash,
    never an input to it. Today's own inputs still knock exactly as before.
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
            # `remaining`, not `minutes`: a sitting finished on a multi-session
            # assignment genuinely changes the pool the day was built from, and a
            # still-proposed board should rebuild around what is left rather than keep
            # offering the work that was just done.
            (c.commitment_id, c.what, c.remaining, c.due_at or "", c.priority,
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
    relief_until: datetime | None = None,
    relief_level: int = 0,
    horizon: runway_mod.Runway | None = None,
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
    clamp = now if (now is not None and now.date() == day) else None
    whole_day_events = (
        list(events) if events is not None
        else capacity_mod.day_events(conn, settings, day)
    )
    yields: list[capacity_mod.Yield] = []
    if relief_until is not None:
        whole_day_events, yields = capacity_mod.relieved(
            whole_day_events, settings, compress=relief_level >= 2
        )
    cap = capacity_mod.compute(
        conn, settings, day, events=whole_day_events, not_before=clamp,
        until=relief_until,
    )
    proposal = Proposal(day=day, tz=cap.tz, capacity=cap)
    proposal.relieved = relief_until is not None
    for given in yields:
        # P18's rule, which relief needs more than any routine ever did: an evening that
        # disappears without a sentence is one the owner discovers at nine o'clock.
        proposal.notes.append(
            f"{given.title} gave up {given.minutes}m so work due now could fit"
            if given.removed
            else f"{given.title} was cut by {given.minutes}m so work due now could fit"
        )
    proposal.fingerprint = inputs_fingerprint(conn, settings, day, at_risk_goals)

    change = timezones.changed_on(settings, day)
    if change:
        # P15. The brief leads with this; the plan records it so the brief can.
        proposal.notes.append(f"Timezone changed {change[0]} → {change[1]}.")

    ahead = tomorrow_preview(conn, day)
    if ahead:
        # Placed before the plannable early-returns on purpose: a fully-booked day is
        # exactly the day that needs to hear the 8am exam is tomorrow.
        shown = "; ".join(ahead[:3]) + ("…" if len(ahead) > 3 else "")
        proposal.notes.append(f"Tomorrow holds: {shown} — leave room to prepare.")

    # Fixed events sit where they sit (docs/04 §1.5 rule 1). The whole day's picture,
    # not the window-clipped list capacity computed with: a 7:15pm dinner and a 7:30am
    # breakfast belong on the plan even though neither spends working capacity. An
    # explicit `events` list stays the whole picture, same contract as `compute`.
    whole_day = whole_day_events
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

    for event in whole_day:
        if event.conflict:
            # P18. A routine the day left no room for, said here rather than swallowed,
            # because "there was no free 45 minutes between 10:30 and 14:30" is a fact
            # about the day the owner can act on — by moving something, or by eating
            # anyway and knowing the plan knows.
            proposal.notes.append(event.conflict)

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

    stale = staleness.stale_ids(conn, day)
    not_yet: set[int] = set()
    on_calendar: set[int] = set()
    pool = candidates(
        conn, settings, day, at_risk_goals or set(), stale,
        not_yet=not_yet, on_calendar=on_calendar,
    )

    # The fortnight, before the day. `runway.allocate` walks every day between here and
    # each obligation's deadline and lays its sittings on the earliest days with room —
    # so today's plan can hold the work that has to start today rather than the work that
    # happens to be oldest, and so the plan can say two weeks early what will not finish.
    #
    # Built from `pool` rather than from its own query on purpose: the runway and the day
    # it advises then cannot disagree about what is open, what is stale, or what has
    # already been done.
    # Computed once for the day, and handed down through the relief passes rather than
    # redone by each of them. It is a forecast of the fortnight — extending *this* evening
    # does not change what next Tuesday can hold — and recomputing it was costing fourteen
    # capacity walks per pass: a plan for a crowded day took 13.3 seconds, three quarters
    # of it spent re-deriving the same answer twice.
    if horizon is None:
        horizon = runway_mod.allocate(
            conn, settings, pool, day,
            not_before=now if (now is not None and now.date() == day) else None,
        )
    if horizon.sittings or horizon.unreachable:
        # Re-derived rather than mutated: `Candidate.priority` is set in `candidates`,
        # and a second writer for it would be a field two functions disagree about.
        # Re-derived with the same out-set, cleared first: a second pass appending to a
        # set the first pass already filled would double-count nothing (it is a set) but
        # would keep an id whose assignment unlocked between the two calls, which cannot
        # happen today and is the sort of thing that becomes true quietly.
        not_yet.clear()
        on_calendar.clear()
        pool = candidates(
            conn, settings, day, at_risk_goals or set(), stale,
            allocated_today=horizon.on(day), not_yet=not_yet, on_calendar=on_calendar,
        )
    if not_yet:
        # The third answer. Before migration 0036 these landed in "did not fit" beside
        # work the day genuinely had no room for, and those are different facts: one is
        # a scheduling failure and the other is a door that has not opened. CHM 113's
        # Act 2 signup was locked until Sep 3 and reported as overflow every morning
        # until it was not.
        proposal.notes.append(
            f"{len(not_yet)} item(s) not yet open — locked until a later date, "
            "not work that would not fit."
        )
    if on_calendar:
        # Said out loud for the same reason as the other two: an obligation that vanishes
        # from the board without explanation reads as one the system has forgotten. This
        # one is the good case — it has a time and a place — and the sentence is what
        # distinguishes it from the two that are not.
        proposal.notes.append(
            f"{len(on_calendar)} item(s) already on the calendar — "
            "the event is the work, and the day is charged for it once."
        )
    held = len(stale)
    if held:
        # P2's rule applied to the gate: a plan that quietly leaves out eighty-three
        # obligations reads as a plan that does not know about them. The sentence says
        # where they went, because the answer is what brings them back.
        proposal.notes.append(
            f"{held} long-lapsed item(s) held back, awaiting your answer on the ask page."
        )
    prefs = preferences_mod.load(conn)
    for warning in prefs.warnings:
        # A preference silently ignored is worse than none — the owner believes it is
        # being applied. Same register as the fragmented-calendar sentence.
        proposal.notes.append(warning)
    # Computed before `select` so the day's budget can be told what the protected block
    # will cost it — see `select`'s note. A protected run the day cannot afford is not
    # placed at all: claiming 90 minutes out of a 60-minute budget is how the arithmetic
    # went wrong in the first place.
    protected = _peak_slot(cap, settings, day)
    if protected is not None and protected.minutes > cap.capacity_minutes:
        protected = None
    scheduled, small, overflow = select(
        pool, cap, settings, prefs,
        protected_minutes=protected.minutes if protected is not None else 0,
    )
    proposal.overflow = overflow
    # After `select`, never before it. The runway walks the fortnight in deadline order
    # against a day's total free minutes; `select` fills today in priority order, with a
    # homework reservation and a protected block charged up front. The two agree on
    # nearly everything and they are not the same algorithm, so the runway can say a quiz
    # does not fit while the day it advised then schedules it — which is what the first
    # run of this did, warning about a 7:00am block printed nine lines above the warning.
    #
    # A plan that contradicts itself in one screen is worse than one that says less
    # (rule 1: two wrong claims and the trust is gone). So the forecast never gets to
    # warn about work today just covered, and `select` — which is what actually writes
    # the day — wins every disagreement.
    _note_runway(proposal, horizon, covered=_covered_today(scheduled, small))
    # Read here, before the protected block pops the first real piece of work off
    # `scheduled` — which is exactly where a coursework item usually goes, so asking
    # later saw an empty homework list and gave a day full of homework a study block
    # on top of it. `divisible` is the discriminator because it is the same one: a
    # divisible estimate is a `coursework` estimate read off the assignment, which is
    # what "homework" means here.
    homework_planned = any(item.divisible for item in scheduled + small)

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
                # Partial when it is partial. The protected slot is a fixed 90 minutes
                # and it takes the first real piece of work, so it is exactly where a
                # long obligation gets truncated — and it said nothing about it, which
                # made "done" on that block look like done with the whole thing.
                "title": _block_title(head, protected.minutes),
                "minutes": protected.minutes,
                "commitment_id": head.commitment_id,
                "goal_id": head.goal_id,
            }
        )
        proposal.protected_placed = True
        for slot in cap.slots:
            if slot.starts_at <= protected.starts_at < slot.ends_at:
                cursor_by_slot[id(slot)] = protected.ends_at

    def room_in(slot: Slot) -> int:
        return int((slot.ends_at - cursor_by_slot[id(slot)]).total_seconds() // 60)

    def place(
        title: str, minutes: int, kind: str, commitment_id: int | None, goal_id: int | None
    ) -> bool:
        """P5, best-fit. Work only ever lands inside a free slot, so it cannot cross a
        fixed event — and it lands in the *tightest* slot that holds it.

        First-fit was the original rule and it wastes the thing a fragmented day has
        least of: a long run. On the owner's 2026-08-24 the 08:00-09:00 hour is the only
        place a 60-minute obligation can go, and first-fit hands it to whichever
        25-minute task is ordered first. Best-fit puts the 25-minute task in the
        25-minute gap and leaves the hour whole, which is the whole difference between
        "does not fit" and "fits".

        Ties break earliest, so the day still fills front to back and two equally tight
        slots give the same answer on every run — `inputs_fingerprint` (0028) hashes a
        plan's inputs and a placement that wandered between runs would look like drift.
        """
        best: Slot | None = None
        for slot in cap.slots:
            room = room_in(slot)
            if room < minutes:
                continue
            if best is None or room < room_in(best):
                best = slot
        if best is None:
            return False
        cursor = cursor_by_slot[id(best)]
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
        cursor_by_slot[id(best)] = end
        return True

    def place_split(
        title: str, minutes: int, kind: str, commitment_id: int | None, goal_id: int | None
    ) -> int:
        """Place what fits, in pieces if it must. Returns the minutes actually placed.

        Owner's ruling, 2026-08-24: "find a way to fit it into the day". A class day is
        fragments — 30, 60, 25, 35, 60 — and an hour and a half of coding asked for a
        hole none of them is. Whole, it vanished; split across the two biggest gaps it
        happens.

        Two rules keep this from turning the day into confetti: no piece is smaller than
        `min_block_minutes` (P4's floor, which exists because a 10-minute fragment of
        anything is not a working session), and the pieces are numbered in the title so a
        block never pretends to be the whole thing — the same honesty `_block_title`
        applies to a clamped sitting.

        Biggest gap first, deliberately, unlike `place`: splitting is what happens when
        nothing fits whole, so the goal is the fewest, longest pieces rather than the
        tightest fit.
        """
        remaining_to_place = minutes
        pieces: list[tuple[Slot, int]] = []
        for slot in sorted(cap.slots, key=lambda sl: (-room_in(sl), sl.starts_at)):
            if remaining_to_place < settings.min_block_minutes:
                break
            piece = min(room_in(slot), remaining_to_place)
            if piece < settings.min_block_minutes:
                continue
            pieces.append((slot, piece))
            remaining_to_place -= piece
        if not pieces:
            return 0
        total = sum(piece for _, piece in pieces)
        for index, (slot, piece) in enumerate(
            sorted(pieces, key=lambda item: item[0].starts_at), start=1
        ):
            cursor = cursor_by_slot[id(slot)]
            end = cursor + timedelta(minutes=piece)
            proposal.blocks.append(
                {
                    "starts_at": cursor.isoformat(),
                    "ends_at": end.isoformat(),
                    "kind": kind,
                    "title": (
                        title if len(pieces) == 1
                        else f"{title} ({index} of {len(pieces)})"
                    ),
                    "minutes": piece,
                    "commitment_id": commitment_id,
                    "goal_id": goal_id,
                }
            )
            cursor_by_slot[id(slot)] = end
        return total

    def largest_free() -> int:
        """The longest run still unspent, after everything placed so far.

        Standing blocks are sized against this rather than against the capacity left
        over, because those are different numbers and only one of them can hold a block.
        On the owner's real 2026-08-26 the day finished with 85 minutes of capacity
        unspent and no gap longer than 60, so a 85-minute Coding block asked for a hole
        that did not exist and `place` returned False — and a standing block that fails
        to place simply vanishes, with nothing said. Shrunk to fit, it lands.
        """
        return max(
            (
                int((slot.ends_at - cursor_by_slot[id(slot)]).total_seconds() // 60)
                for slot in cap.slots
            ),
            default=0,
        )

    def standing(title: str, kind: str, wanted: int) -> None:
        """A synthetic block: capacity-bounded, fit-bounded, never rolled.

        Shared by Study and Coding, which differ only in their condition — see
        `Settings.coding_block_minutes` for why that is two settings rather than a
        config grammar.
        """
        if wanted <= 0:
            return
        if _pressed(proposal, horizon):
            # Owner's ruling, 2026-08-27: "reduce time for other things". Study and
            # Coding are the two standing blocks, and they are the clearest case of an
            # "other thing" — neither has a deadline, neither is an obligation anybody
            # made, and both are placed out of whatever `select` did not spend.
            #
            # That last part is how they came to outrank work due today. `select` decides
            # a budget and hands back the rest as overflow; these then spend the leftovers
            # regardless. Measured on 2026-09-01: a 90-minute Coding block was placed at
            # 8:45pm while thirty items due that day, including the Gilgamesh reading for
            # the next morning's seminar, were in overflow. The block is not wrong — the
            # order was.
            return
        room = min(wanted, cap.capacity_minutes - proposal.planned_minutes)
        if room < settings.min_block_minutes:
            return
        # Split across gaps rather than shrink to the biggest one. Sizing to
        # `largest_free()` alone was the 2026-08-24 fix for a block that vanished; it
        # still gave a 90-minute setting whatever the day's longest hole happened to be,
        # so a day of 45-minute fragments bought 45 minutes of coding and threw the rest
        # away while the gaps sat empty.
        place_split(title, room, kind, None, None)

    for item in scheduled:
        minutes = item.planned_minutes or item.sitting(settings)
        title = _block_title(item, minutes)
        if place(title, minutes, "work", item.commitment_id, item.goal_id):
            continue
        # Nothing holds it whole. Divisible work — a `coursework` estimate read off a
        # deliverable measured in pages, chapters or runtime — is done across sittings by
        # construction, so it may take the day in pieces rather than not at all. Anything
        # else is planned whole or not at all: "Move-in: Willow Hall 502" is three hours
        # of one thing, and cutting it into fragments would be a plan that cannot happen.
        if item.divisible and place_split(
            title, minutes, "work", item.commitment_id, item.goal_id
        ):
            continue
        proposal.overflow.append(item)

    if small:
        total = max(
            settings.min_block_minutes,
            sum(s.planned_minutes or max(1, s.minutes) for s in small),
        )
        title = f"Small items ({len(small)})"
        # Whole first, in pieces second. A small-items block is P4's batch of *separate*
        # obligations — a form, two replies, a quiz — so it is divisible by construction
        # in a way a single 90-minute deliverable is not, and refusing to split it was
        # costing the day far more than the tidiness was worth.
        #
        # Measured on 2026-09-01: ten small items were charged ~200 minutes of budget by
        # `select`, then failed to find one contiguous run because the protected block and
        # the day's real work had taken the long slot first — so all ten went to overflow
        # AND their minutes stayed spent. The day reported 565 minutes of capacity, placed
        # 375, and left thirty items due that day unplaced, including the Gilgamesh
        # reading for the next morning's seminar. That is the "2.25 hours available that
        # can be planned for" the owner was looking at.
        if not place(title, total, "small", None, None) and not place_split(
            title, total, "small", None, None
        ):
            proposal.overflow.extend(small)

    # P20, docs/04 §1.9. A day should hold study time as well as homework.
    # Homework is what the planner already does — coursework commitments scheduled by
    # name — so this is only for the day that has none of it.
    #
    # A block, not a commitment. Nothing is written to the ledger, nothing rolls over,
    # and there is nothing to mark done but the block itself — a plan may say "read"
    # without inventing an obligation the owner never made. It is capacity-bounded like
    # everything else (P1): what is left after the real work, never more.
    #
    # Its own kind, not `work`. `rollover.open_blocks` rolls every pending
    # work/protected/small block into tomorrow, and an hour of reading nobody did is not
    # a debt — rolled, it would arrive tomorrow as an obligation the owner never made,
    # and P11 would eventually ask them whether to drop it. It still counts toward
    # `planned_minutes`: the time is genuinely spent.
    if not homework_planned:
        standing("Study", "study", settings.study_block_minutes)

    # The coding block. Owner's ruling, 2026-08-24 — same mechanism as Study, and
    # unconditional: this is time for the owner's own building (OrgTruth, Avorio), which
    # produces no commitment for the board to schedule and so would otherwise be the one
    # lane that never appears on a day at all.
    standing("Coding", "coding", settings.coding_block_minutes)

    if proposal.overflow:
        # P2. Never silently truncate.
        proposal.notes.append(f"{len(proposal.overflow)} item(s) did not fit.")
        _note_priority_conflict(proposal, day)

    proposal.blocks.sort(key=lambda b: str(b["starts_at"]))

    if relief_level < 2 and _needs_relief(proposal, horizon):
        # Owner's ruling, 2026-08-27: "if things dont fit remove relax time and mcat
        # study time or reduce time for other things."
        #
        # A second pass rather than a wider first one, so a day that fits is planned
        # exactly as it was and nothing is taken from anybody who did not need it taken.
        # `_needs_relief` is the whole safety property — see its docstring.
        until = capacity_mod.relief_window_end(settings, day)
        if until is not None and until > timezones.window_on(settings, day)[1]:
            relieved_plan = propose(
                conn, settings, day,
                at_risk_goals=at_risk_goals, events=events, now=now,
                relief_until=until, relief_level=relief_level + 1, horizon=horizon,
            )
            # Only if it bought something. Relief is spending the owner's evening and
            # their gym, and `_needs_relief` stays true for work no amount of evening can
            # reach — three duplicate welcome surveys due today are unreachable at every
            # level, and escalating on their account would cut the gym every day for the
            # rest of the semester and place nothing extra. So each level has to pay for
            # itself: fewer deadline-pressed items left over than the pass before it, or
            # the cheaper plan stands.
            if _pressed(relieved_plan, horizon) < _pressed(proposal, horizon):
                return relieved_plan
    return proposal


def _pressed(proposal: Proposal, horizon: runway_mod.Runway) -> int:
    """How much work that cannot wait this plan failed to place."""
    unreachable = {c.commitment_id for c in horizon.unreachable}
    return sum(
        1
        for item in proposal.overflow
        if item.priority <= PRIORITY_DUE_TODAY or item.commitment_id in unreachable
    )


def _needs_relief(proposal: Proposal, horizon: runway_mod.Runway) -> bool:
    """Is the work that did not fit work that cannot wait?

    This is the load-bearing line of the whole relief pass, and the reason it is not
    simply "if anything overflowed". The owner's board carries 255 hours against a day of
    two or three free ones, so *something* overflows every single day and always will —
    under that trigger Relax would be deleted every evening for the rest of the semester,
    which is the opposite of what "if things don't fit" means when a person says it.

    So relief fires only for work with a deadline pressing on it: overdue, due today, or
    named by the fortnight's allocation as not finishing before it is owed. Those are the
    three things an evening is worth spending on. The rest of the pile waits, exactly as
    it did.
    """
    return _pressed(proposal, horizon) > 0


def _covered_today(scheduled: list[Candidate], small: list[Candidate]) -> set[int]:
    """Ids today's plan finishes off, not merely touches.

    `planned_minutes` is a sitting, and a 344-minute milestone getting 90 of them today
    is still work that has to finish somewhere — so it stays eligible for the warning.
    Only the obligations the day actually closes are struck from it.
    """
    return {
        item.commitment_id
        for item in scheduled + small
        if item.planned_minutes >= item.remaining
    }


def _note_runway(
    proposal: Proposal, horizon: runway_mod.Runway, covered: set[int] | None = None
) -> None:
    """The two sentences only a fortnight-wide view can write.

    This replaces the useful half of "216 item(s) did not fit". That number counts a
    backlog against one Thursday and says nothing a reader can act on — of course three
    hundred obligations do not fit in an afternoon. What is worth waking up to is the
    thing that does not fit *before it is owed*, and it is worth hearing two weeks early,
    while there is still something to do about it.

    Named, capped at three, and the count carries the rest. A sentence listing eleven
    assignments is a sentence nobody finishes reading, and the ones after the third are
    the ones with the most time left anyway.
    """
    if horizon.unreadable:
        # Rule 5: a day that could not be measured cost its own allocation and nothing
        # else. Said out loud, because a silently missing day makes the rest of the
        # forecast quietly optimistic.
        days = ", ".join(d.isoformat() for d in horizon.unreadable[:3])
        proposal.notes.append(
            f"{len(horizon.unreadable)} day(s) of the next fortnight could not be read "
            f"({days}) — the runway below is optimistic by whatever they hold."
        )
    missing = [
        c for c in horizon.unreachable if c.commitment_id not in (covered or set())
    ]
    if not missing:
        return
    named = "; ".join(c.what for c in missing[:3])
    proposal.notes.append(
        f"{len(missing)} item(s) do not finish before they are due, at the "
        f"capacity of the next {runway_mod.DEFAULT_HORIZON_DAYS} days: {named}"
        + ("…" if len(missing) > 3 else "")
        + " — start one early, cut its scope, or move the date."
    )


def _note_priority_conflict(proposal: Proposal, day: date) -> None:
    """When something near its deadline did not fit, say what is holding the time.

    Owner's ruling, 2026-08-24: "there should be a priority list in terms of conflicts."
    `plan/priority.py` is the list; this is what it does when a conflict actually happens.

    It names rather than acts, and that boundary is the point. Tier 2 sits above tier 3,
    so the list does say a paper due tomorrow outranks the gym — but a routine is the
    owner's own decision about their day, and a planner that quietly deleted dinner to fit
    an essay is the surface nobody trusts twice. The plan states the collision in tier
    order, cheapest thing to give up first, and the owner moves one.

    Fixed events are never listed: they are tier 1 and the list says they do not move.
    """
    from backglass.plan import priority

    imminent = [
        item for item in proposal.overflow
        if item.due_at
        and (date.fromisoformat(str(item.due_at)[:10]) - day).days
        <= priority.IMMINENT_HOURS // 24
    ]
    if not imminent:
        return

    #: What a block costs on the list, lowest priority first — the order to give things up
    #: in. Work blocks are ranked by their own text, so an errand offers itself before
    #: coursework does.
    holders: list[tuple[int, str, int]] = []
    for block in proposal.blocks:
        kind = str(block["kind"])
        minutes = int(block.get("minutes") or 0)
        if kind in ("fixed", "allday") or minutes <= 0:
            continue
        if kind == "routine":
            rank = 3
        elif kind in ("coding", "study"):
            rank = 5
        else:
            rank = priority.rank_of(str(block["title"]))
        holders.append((rank, str(block["title"]), minutes))
    if not holders:
        return

    holders.sort(key=lambda h: (-h[0], -h[2]))
    named = ", ".join(
        f"{title} ({minutes}m, tier {rank})" for rank, title, minutes in holders[:3]
    )
    proposal.notes.append(
        f"{len(imminent)} item(s) due within {priority.IMMINENT_HOURS}h did not fit. "
        f"Lower on the priority list, holding time today: {named}. "
        "Nothing was moved for them — that is yours to decide."
    )


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
