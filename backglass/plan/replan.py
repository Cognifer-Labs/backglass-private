"""Dynamic replanning: the day's plan follows the day, within a decided boundary.

The 05:45 job plans the morning's world; the world keeps moving. A commitment lands at
10:00, a dinner moves, a preference changes — and until now the plan silently described
a day that no longer existed, which is how a planner loses the owner's trust without
ever being wrong loudly.

This is the decided resolution (tasks/todo.md increment 6) of the conflict between
"dynamic scheduling" and catchup's "never replace a live plan":

    A plan still `proposed` is the system's, and the system may re-plan it.
    An `accepted` plan is the owner's, and the system may only knock.

Drift is detected by fingerprint, not by diffing blocks: `propose` stamps every plan
with a clock-free hash of its inputs (planner.inputs_fingerprint), and a sync that
changed the world changes the hash. A plan from before fingerprints existed (NULL) is
never treated as drifted — migration day must not replace every standing plan.

Runs on the sync path beside catchup: catchup fills the hole where a plan is missing,
this refreshes the plan that exists. Both are quiet in the normal case.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from backglass.config import Settings
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class Replanned:
    day: date
    action: str  # replaced | drifted
    detail: str


def run(
    conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None
) -> Replanned | None:
    """Compare today's live plan against today's world; act inside the boundary.

    Returns what happened, or None when there is nothing to say — no plan (catchup's
    job), no fingerprint (pre-migration plan), or no drift (the normal case).
    """
    from backglass import notify
    from backglass.goals import health
    from backglass.plan import planner, timezones

    if now is None:
        now = timezones.local_now(settings)
    day = now.date()

    row = conn.execute(
        "SELECT id, status, inputs_fingerprint FROM day_plan"
        " WHERE user_id = ? AND local_date = ? AND status != 'superseded'"
        " ORDER BY id DESC LIMIT 1",
        (USER_ID, day.isoformat()),
    ).fetchone()
    if row is None or row["inputs_fingerprint"] is None:
        return None

    at_risk = health.at_risk_goal_ids(conn, settings, day)
    current = planner.inputs_fingerprint(conn, settings, day, at_risk)
    if current == str(row["inputs_fingerprint"]):
        return None

    if str(row["status"]) == "proposed":
        # The system's own plan: regenerate, supersede, say so once. The clock clamp
        # rides along, so an afternoon replacement plans the hours that are left.
        proposal = planner.propose(conn, settings, day, at_risk_goals=at_risk, now=now)
        planner.persist(conn, settings, proposal)
        detail = (
            f"{len(proposal.blocks)} block(s), {len(proposal.overflow)} did not fit"
        )
        notify.record(
            conn, settings,
            kind="plan-replaced", subject_key=day.isoformat(),
            title="Today's plan was updated",
            body="The day changed since it was planned — the new plan is on /schedule.",
            now=now,
        )
        return Replanned(day=day, action="replaced", detail=detail)

    # The owner's plan: knock, never clobber. Once per day by the notification
    # table's own dedup.
    notify.record(
        conn, settings,
        kind="plan-drift", subject_key=day.isoformat(),
        title="Your day changed after you accepted the plan",
        body="Something new landed. The accepted plan stands — Replan on /schedule"
             " if you want it rebuilt.",
        now=now,
    )
    return Replanned(day=day, action="drifted", detail="accepted plan left standing")
