"""The morning surfaces, produced on a machine that was asleep at 05:45.

`StartCalendarInterval` defers a missed firing to the next wake, so on a laptop that
sleeps overnight the 05:45 plan job and the 06:00 brief job run whenever the lid is next
opened. Read from the logs on 2026-08-15 that was 17:22 and 17:37 — the plan for the day
arrived after the day, and the two-minute morning read arrived at dinner.

`com.backglass.plan-catchup` was built for this and does not reach it: `RunAtLoad` fires
at *login*, so a machine that sleeps and wakes never triggers it. What does run on wake
is the every-1800s sync job, and that is where this hooks in.

Two properties make it safe to run on every sync:

  - **It only ever fills a hole.** A day that already has a live plan, or a brief already
    generated for today, is left alone — so an accepted or hand-edited plan is never
    replaced, and nobody receives two briefs.
  - **It never runs early.** Each surface has an hour it is owed at, and before that hour
    nothing is missing. Generating tomorrow's plan at 22:00 tonight would be wrong, not
    helpful.

It is deliberately not a scheduler. The scheduled jobs remain the primary path and this
is the net underneath them; if the machine is awake at 05:45 nothing here ever fires.

The retrieval index is caught up here too, for the same reason rather than by analogy: it
was work that had no job at all. `search index` was manual-only — nothing in `sync.py`,
nothing in any launchd template, ever called it — so the backlog grew from 8 documents to
36 in a week and the only thing that noticed was a `state` field nobody runs unprompted.
Unlike the two surfaces above it has no hour, so it runs on every sync.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from backglass.config import Settings
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class Filled:
    """One surface this run produced, for the caller to report."""

    surface: str  # plan | brief
    day: date
    detail: str


def _local_now(settings: Settings) -> datetime:
    from backglass.plan import timezones

    return timezones.local_now(settings)


def _owed(now: datetime, at: str) -> bool:
    """Has the `HH:MM` this surface is owed at already passed today?

    The hour comes from the same setting the launchd template is rendered from, so the
    net and the job can never disagree about when the hole opens.
    """
    from backglass.schedule import _hhmm

    hour, minute = _hhmm(at)
    return (now.hour, now.minute) >= (hour, minute)


def plan_is_missing(conn: sqlite3.Connection, day: date) -> bool:
    from backglass.plan import planner

    return planner.current_plan_id(conn, day) is None


def brief_is_missing(conn: sqlite3.Connection, day: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM brief WHERE user_id = ? AND generated_for_date = ? AND kind = 'daily'",
        (USER_ID, day.isoformat()),
    ).fetchone()
    return row is None


def run(
    conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None
) -> list[Filled]:
    """Fill whatever the morning owed and did not deliver. Returns what it produced.

    Failures degrade rather than block, per CLAUDE.md rule 5 — this runs inside the sync
    job, and a brief that cannot be generated must not take the sync down with it. The
    caller logs what came back; an empty list is the normal case and says nothing.
    """
    now = now or _local_now(settings)
    day = now.date()
    filled: list[Filled] = []

    if _owed(now, settings.plan_at) and plan_is_missing(conn, day):
        try:
            filled.append(_fill_plan(conn, settings, day, now))
        except Exception as exc:  # noqa: BLE001 - rule 5: degrade, never block
            filled.append(Filled("plan", day, f"failed: {type(exc).__name__}: {exc}"))

    if _owed(now, settings.brief_at) and brief_is_missing(conn, day):
        try:
            filled.append(_fill_brief(conn, settings, day))
        except Exception as exc:  # noqa: BLE001 - rule 5, and B6 one layer up
            filled.append(Filled("brief", day, f"failed: {type(exc).__name__}: {exc}"))

    indexed = _index_backlog(conn, settings)
    if indexed:
        filled.append(Filled("retrieval", day, f"{indexed} document(s) indexed"))

    return filled


def _index_backlog(conn: sqlite3.Connection, settings: Settings) -> int:
    """Embed whatever retrieval cannot reach yet. Returns how many were added.

    `search index` was a manual command and nothing ever called it — not `sync.py`, not
    any launchd template. That is the whole reason the backlog grew from 8 documents to
    36 in a week while nobody did anything wrong: the drop folder's contracts and letters
    were being collected and never became findable, and the only thing that noticed was
    `backglass state`, which nobody runs unprompted.

    Safe on a thirty-minute timer by construction rather than by care: `index()` is
    bounded per call, `INSERT OR IGNORE` against a unique index, and resumable because
    that index *is* the watermark — interrupting it costs nothing and re-running it
    continues. The model is local, so this cannot touch the spend cap.

    Silent on failure, deliberately. Retrieval is additive by CLAUDE.md's ruling: if the
    embedding endpoint is down, every existing surface must still be correct, and a sync
    that fails because ollama is not running would be the additive layer becoming
    load-bearing through the back door.
    """
    try:
        from backglass import search

        return search.index(conn, settings)
    except Exception:  # noqa: BLE001 — no endpoint, no model, no vectors: all the same
        return 0


def _fill_plan(
    conn: sqlite3.Connection, settings: Settings, day: date, now: datetime
) -> Filled:
    from backglass.goals import health
    from backglass.plan import planner

    proposal = planner.propose(
        conn,
        settings,
        day,
        at_risk_goals=health.at_risk_goal_ids(conn, settings, day),
        now=now,
    )
    planner.persist(conn, settings, proposal)
    return Filled(
        "plan",
        day,
        f"{len(proposal.blocks)} block(s), {len(proposal.overflow)} did not fit",
    )


def _fill_brief(conn: sqlite3.Connection, settings: Settings, day: date) -> Filled:
    """Generate and persist. Delivery is left to the scheduled job on purpose.

    Sending is an outbound side effect with an hour attached to it — the brief says
    "morning" and the owner agreed to receive it at 06:00, not at whatever moment a sync
    happened to notice the hole. Generating it makes it readable at /brief and in the
    app, which is the part that was silently missing.

    Stated precisely, because the near-miss version of this sentence is a false claim:
    `daily.persist` upserts on (user_id, generated_for_date, kind), so the deferred 06:00
    job overwrites this row rather than adding a second one. It does **not** check
    whether a brief already exists — there is no such guard in the CLI — so on a sleeping
    machine the mail still goes out whenever that job finally fires. This closes the hole
    where the brief could not be read at all; it does not move the delivery hour, and
    nothing here should be read as claiming it does.
    """
    from backglass.brief import daily, render

    built = daily.build_for(conn, settings, day)
    daily.persist(conn, built, render.to_markdown(built, base_url=settings.dashboard_base_url))
    return Filled("brief", day, f"{built.word_count()} words, generated not sent")
