"""Building the daily brief from ledger state. docs/05.

B5: "Generated from ledger state only. No live API calls at send time." Every function
here takes a connection and returns rows. Nothing in this module can reach the network,
which is what makes a 06:00 send independent of whether Gmail is up.

Sections 1, 2, 3, 6 and 7 depend on the day planner and the goal engine, which are
Phase 4. Their builders are written and query the real tables; those tables are simply
empty until then, so the sections are omitted by B3. That is the correct behaviour and
not a stub — there genuinely is no capacity line before there is a capacity model.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backglass.brief.model import Brief, LedgerRef, Line, Note, Section, SourceRef
from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID

#: docs/05 §4. Two days, in the sense of calendar days, because the reader thinks in days.
SLIPPING_HORIZON_DAYS = 2
#: docs/05 §7. "Items rolling over a third time."
ROLLOVER_THRESHOLD = 3
#: docs/07 §Instagram. How far ahead the Friend plans section looks. Wider than
#: slipping's two days because a Saturday plan made on Monday should be visible all week.
FRIEND_PLANS_HORIZON_DAYS = 14

#: A friend plan that is *not* one of these demotes to the Friend plans section; one
#: that is stays in the normal sections at full priority. Deterministic on purpose —
#: a keyword test is checkable against the `what` it matched, a model judgment is not.
_SPECIAL_EVENT = re.compile(
    r"\b(birthday|b[- ]?day|anniversary|wedding|graduation|farewell|engagement"
    r"|baby\s*shower|housewarming)\b",
    re.IGNORECASE,
)


def _demoted_friend_plan(row: dict[str, Any]) -> bool:
    """True when a commitment's evidence is an Instagram DM and nothing about it is
    special — the owner's rule: friend plans ride low unless it's a birthday or a
    special event."""
    source = str(row["source"])
    if source != "instagram" and not source.startswith("instagram:"):
        return False
    return not _SPECIAL_EVENT.search(str(row["what"] or ""))


def today_in(tz: str) -> date:
    """The owner's local date. The brief is a local-morning object, not a UTC one."""
    return datetime.now(ZoneInfo(tz)).date()


def _rows(conn: sqlite3.Connection, name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return conn.execute(query(name), params).fetchall()


def _source_of(row: dict[str, Any]) -> SourceRef:
    item_id = row.get("source_item_id")
    return SourceRef(
        source=str(row["source"]),
        external_id=str(row["source_external_id"]),
        occurred_at=str(row["source_occurred_at"]),
        title=row.get("source_title"),
        source_item_id=int(item_id) if item_id is not None else None,
    )


def _due_phrase(due_at: Any, today: date) -> str:
    if not due_at:
        return "no date"
    due = date.fromisoformat(str(due_at)[:10])
    delta = (due - today).days
    if delta < 0:
        return f"overdue {abs(delta)}d"
    if delta == 0:
        return "due today"
    if delta == 1:
        return "due tomorrow"
    return f"due {due.strftime('%a %-d %b')}"


def _status_of(due_at: Any, today: date) -> str:
    """design-system.md §4. Color means state, and nothing else gets an ink."""
    if not due_at:
        return "open"
    due = date.fromisoformat(str(due_at)[:10])
    if due < today:
        return "overdue"
    if due == today:
        return "due_today"
    return "slipping"


# ── failure states, above everything ──────────────────────────────────────
# docs/05: "Each of these is more valuable than any section it displaces."


def _staleness_lines(
    conn: sqlite3.Connection,
    settings: Settings,
    today: date,
    now: datetime | None,
    section: Section,
) -> None:
    """The two claims nothing else in the brief can make: the ledger stopped moving,
    and no plan exists for the day this brief is about.

    Both are sourced like everything else — the run row and the day the plan was owed.
    B2 has no exception for infrastructure; "your scheduler is dead" is a claim about
    the world and the reader gets to check it.
    """
    from backglass import heartbeat as heartbeat_mod

    beat = heartbeat_mod.read(conn, settings, today, now)

    if beat.never_ran:
        section.lines.append(
            Line(
                text="Sync has never run — nothing in this brief comes from your mail.",
                provenance=LedgerRef("sources", "sync", "run log · empty"),
                status="overdue",
            )
        )
    elif beat.stale:
        # The failing sources are the *why*: a stale ledger with Gmail broken is an
        # auth problem, a stale ledger with every source healthy is a dead launchd job,
        # and the fix differs. Naming them here costs six words and saves the hunt.
        tail = f" — {beat.failed_phrase}" if beat.failed_phrase else ""
        section.lines.append(
            Line(
                text=f"Ledger last updated {beat.age_phrase}{tail}. This brief may be stale.",
                provenance=LedgerRef(
                    "runs", str(beat.last_run_id), f"run · {str(beat.last_run_at)[:10]}"
                ),
                status="overdue",
            )
        )

    if beat.plan_missing:
        section.lines.append(
            Line(
                text="No plan for today — the 05:45 planner did not run.",
                provenance=LedgerRef("plans", today.isoformat(), f"day plan · {today}"),
                status="overdue",
            )
        )


def failure_section(
    conn: sqlite3.Connection,
    today: date,
    settings: Settings,
    now: datetime | None = None,
) -> Section:
    section = Section(priority=0, title="Attention")

    # docs/11 §8, the silent failure: a brief generated from a ledger that stopped
    # updating three days ago reads exactly like a quiet week. Said first, because every
    # line below it is only as current as this one. `now` is injected so the claim is
    # testable without a wall clock.
    _staleness_lines(conn, settings, today, now, section)

    for row in _rows(
        conn, "brief_source_health", {"user_id": USER_ID, "today": today.isoformat()}
    ):
        days = int(row["days_failing"] or 0)
        age = "today" if days == 0 else f"{days}d ago"
        section.lines.append(
            Line(
                text=f"{row['source']} {row['status']} since {age} — this brief is incomplete.",
                provenance=LedgerRef(
                    "sources", str(row["source"]), f"credential · {row['source']}"
                ),
                status="overdue",
            )
        )

    # docs/05 §Failure states: "No plan could be generated because the day is fully
    # booked: say that rather than showing an empty plan." A day of back-to-back fixed
    # events renders as a full Today section and an empty everything-else, which reads as
    # a normal day. It is not one, and the difference is worth a line at the top.
    plan = conn.execute(
        "SELECT p.id, p.local_date, p.overflow_count, "
        "  (SELECT COUNT(*) FROM plan_block b WHERE b.day_plan_id = p.id "
        "   AND b.kind IN ('work', 'protected')) AS work_blocks "
        "FROM day_plan p WHERE p.user_id = ? AND p.local_date = ? AND p.status != 'superseded' "
        "ORDER BY p.id DESC LIMIT 1",
        (USER_ID, today.isoformat()),
    ).fetchone()
    if plan is not None and not int(plan["work_blocks"] or 0):
        overflow = int(plan["overflow_count"] or 0)
        plural = "s" if overflow != 1 else ""
        tail = f" {overflow} item{plural} did not fit." if overflow else ""
        section.lines.append(
            Line(
                text=f"Fully booked — no deep work slot today.{tail}",
                provenance=LedgerRef(
                    "plans", str(plan["local_date"]), f"day plan · {plan['local_date']}"
                ),
                status="slipping",
            )
        )

    last = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
    if last and last["degraded"]:
        section.lines.append(
            Line(
                text="Spend cap reached — extraction paused, triage only. New commitments "
                "are not being extracted.",
                provenance=LedgerRef(
                    "runs", str(last["id"]), f"run · {str(last['started_at'])[:10]}"
                ),
                status="overdue",
            )
        )
    return section


# ── the eight sections of docs/05 ─────────────────────────────────────────


def timezone_section(conn: sqlite3.Connection, today: date, settings: Settings) -> Section:
    """docs/05 §1. Only when today's active zone differs from yesterday's.

    Reads day_plan.tz, which the planner writes in Phase 4. Two plans are needed before
    there can be a change, so this is silent until then.
    """
    section = Section(priority=1, title="Timezone change")
    rows = conn.execute(
        "SELECT local_date, tz FROM day_plan WHERE user_id = ? AND local_date <= ? "
        "ORDER BY local_date DESC LIMIT 2",
        (USER_ID, today.isoformat()),
    ).fetchall()
    if len(rows) < 2 or rows[0]["tz"] == rows[1]["tz"]:
        return section

    now_tz, was_tz = str(rows[0]["tz"]), str(rows[1]["tz"])
    window = settings.working_window
    section.lines.append(
        Line(
            text=f"Timezone changed {was_tz} → {now_tz}. Working window {window} {now_tz}.",
            provenance=LedgerRef(
                "plans", str(rows[0]["local_date"]), f"day plan · {rows[0]['local_date']}"
            ),
        )
    )
    return section


def plan_section(conn: sqlite3.Connection, today: date) -> Section:
    """docs/05 §2. Proposed blocks from the day planner, protected block marked."""
    section = Section(priority=2, title="Today")
    rows = conn.execute(
        "SELECT b.id, b.starts_at, b.ends_at, b.kind, b.title, b.commitment_id, p.local_date "
        "FROM plan_block b JOIN day_plan p ON p.id = b.day_plan_id "
        "WHERE p.user_id = ? AND p.local_date = ? AND p.status != 'superseded' "
        "ORDER BY b.starts_at",
        (USER_ID, today.isoformat()),
    ).fetchall()
    for row in rows:
        mark = " (protected)" if row["kind"] == "protected" else ""
        start, end = str(row["starts_at"])[11:16], str(row["ends_at"])[11:16]
        section.lines.append(
            Line(
                text=f"{start}–{end} {row['title']}{mark}",
                provenance=LedgerRef(
                    "plans", str(row["local_date"]), f"day plan · {row['local_date']}"
                ),
                status="protected" if row["kind"] == "protected" else None,
                commitment_id=row["commitment_id"],
            )
        )
    return section


def capacity_section(conn: sqlite3.Connection, today: date) -> Section:
    """docs/05 §3. One sentence, and only one."""
    section = Section(priority=3, title="Capacity")
    row = conn.execute(
        "SELECT id, local_date, capacity_minutes, planned_minutes, overflow_count "
        "FROM day_plan WHERE user_id = ? AND local_date = ? AND status != 'superseded' "
        "ORDER BY id DESC LIMIT 1",
        (USER_ID, today.isoformat()),
    ).fetchone()
    if row is None:
        return section

    def hm(minutes: Any) -> str:
        total = int(minutes or 0)
        return f"{total // 60}h {total % 60:02d}m"

    overflow = int(row["overflow_count"] or 0)
    tail = f", {overflow} item{'s' if overflow != 1 else ''} did not fit" if overflow else ""
    section.lines.append(
        Line(
            text=(
                f"{hm(row['capacity_minutes'])} available, "
                f"{hm(row['planned_minutes'])} planned{tail}."
            ),
            provenance=LedgerRef(
                "plans", str(row["local_date"]), f"day plan · {row['local_date']}"
            ),
        )
    )
    return section


def slipping_section(conn: sqlite3.Connection, today: date, settings: Settings) -> Section:
    section = Section(priority=4, title="Slipping")
    horizon = today + timedelta(days=SLIPPING_HORIZON_DAYS)
    for row in _rows(
        conn,
        "brief_slipping",
        {
            "user_id": USER_ID,
            "horizon": horizon.isoformat(),
            "confidence_threshold": settings.confidence_threshold,
        },
    ):
        if _demoted_friend_plan(row):
            continue  # rides in friend_plans_section instead
        who = f" to {row['counterparty']}" if row["counterparty"] else ""
        section.lines.append(
            Line(
                text=f"{row['what']}{who}. {_due_phrase(row['due_at'], today)}.",
                provenance=_source_of(row),
                status=_status_of(row["due_at"], today),
                commitment_id=int(row["id"]),
            )
        )
    return section


def awaiting_section(conn: sqlite3.Connection, today: date, settings: Settings) -> Section:
    section = Section(priority=5, title="Awaiting others")
    for row in _rows(
        conn,
        "brief_awaiting",
        {
            "user_id": USER_ID,
            "today": today.isoformat(),
            "confidence_threshold": settings.confidence_threshold,
        },
    ):
        if _demoted_friend_plan(row):
            continue  # rides in friend_plans_section instead
        who = row["counterparty"] or "unknown"
        age = int(row["age_days"] or 0)
        section.lines.append(
            Line(
                text=f"{who}: {row['what']}. {age}d.",
                provenance=_source_of(row),
                status="awaiting",
                commitment_id=int(row["id"]),
            )
        )
    return section


def goal_section(conn: sqlite3.Connection, today: date, settings: Settings) -> Section:
    """docs/05 §6. Active goals, target progress, staleness or risk where flagged.

    G11: staleness and risk are computed independently and never merged. A goal can be
    fresh and at risk, or stale and on track, and those need different sentences — so this
    section can emit both for the same goal rather than one blended verdict.
    """
    from backglass.goals import health
    from backglass.goals import reviews as reviews_mod
    from backglass.goals import targets as targets_mod

    section = Section(priority=6, title="Goals")

    # Today's spaced-repetition load, one line, only when a due
    # snapshot exists and carries work (B3 — a quiet day says nothing). Provenance
    # is the snapshot item itself: the claim "140 due" is checkable against it.
    snapshot = reviews_mod.due_snapshot(conn, today)
    if snapshot and snapshot["due"]:
        minutes, _ = reviews_mod.review_minutes(conn, today)
        run = reviews_mod.streak(conn, settings, today)
        text = f"Reviews: {snapshot['due']} due (~{minutes} min)."
        if run:
            text = f"Reviews: {snapshot['due']} due (~{minutes} min) · {run}-day streak."
        section.lines.append(
            Line(
                text=text,
                provenance=SourceRef(
                    source=snapshot["source"],
                    external_id=snapshot["external_id"],
                    occurred_at=snapshot["occurred_at"],
                    title=snapshot["title"],
                    source_item_id=int(snapshot["item_id"]),
                ),
            )
        )

    for target in targets_mod.progress(conn, settings, today):
        if target.complete:
            continue
        # Weekly progress is a cadence concept. A milestone target ("sit the MCAT")
        # has no meaningful "0 this week" — it reaches the brief through staleness
        # and risk below, not through a weekly count it will never have. Without
        # this, one started roadmap floods the section with a line per milestone.
        if not target.weekly_count:
            continue
        count = f"{target.done_this_week}/{target.weekly_count}"
        section.lines.append(
            Line(
                text=f"{target.goal_title} — {target.title}: {count} this week.",
                provenance=LedgerRef(
                    "goals", str(target.goal_id), f"goal · {target.goal_title}"
                ),
                status="slipping" if target.done_this_week == 0 else None,
            )
        )

    # G12. A day count, never a bare colour.
    for stale in health.staleness(conn, settings, today):
        if stale.level == "fresh":
            continue
        section.lines.append(
            Line(
                text=f"{stale.goal_title}: {stale.chip()}.",
                provenance=LedgerRef("goals", str(stale.goal_id), f"goal · {stale.goal_title}"),
                status="overdue" if stale.level == "serious" else "slipping",
            )
        )

    # G13. A projected date against the target date, in words.
    for goal in health.risk(conn, settings, today):
        if not goal.at_risk:
            continue
        sentence = goal.sentence()
        if sentence:
            section.lines.append(
                Line(
                    text=sentence,
                    provenance=LedgerRef(
                        "goals", str(goal.goal_id), f"goal · {goal.goal_title}"
                    ),
                    status="slipping",
                )
            )
    return section


def rollover_section(conn: sqlite3.Connection, today: date, settings: Settings) -> Section:
    """docs/05 §7 and docs/04 P11.

    docs/04 §5: the drop-or-do question fires "exactly once". `flagged_for_question` only
    returns items that have not been asked about, and `build` marks them asked once the
    brief is assembled — so a brief regenerated twice for the same day does not ask twice.
    """
    from backglass.plan import rollover as rollover_mod

    section = Section(priority=7, title="Rolling over")
    for row in rollover_mod.flagged_for_question(conn, settings):
        section.lines.append(
            Line(
                text=(
                    f"{row['what']} — rolled {row['rollover_count']}x. "
                    "Is this going to happen, or should it be dropped?"
                ),
                provenance=SourceRef(
                    source=str(row["source"]),
                    external_id=str(row["external_id"]),
                    occurred_at=str(row["occurred_at"]),
                    title=row["title"],
                    source_item_id=int(row["source_item_id"]),
                ),
                status="slipping",
                commitment_id=int(row["id"]),
            )
        )
    return section


def follow_up_section(conn: sqlite3.Connection, today: date, settings: Settings) -> Section:
    """Phase 6. Curated people going quiet — at most three, coldest first.

    Provenance is the most recent interaction's source item, which is the evidence for
    the claim being made ("you have not touched this relationship since then"). A
    curated profile with no interactions ever has no evidence to cite, so it appears on
    the People page but never here — a claim with no source does not ship (rule 1).
    """
    from backglass.people import touch

    section = Section(priority=8, title="Follow up")
    for t in touch.needing_follow_up(conn, settings, today)[:3]:
        assert t.source_row is not None  # needing_follow_up guarantees it
        who = t.name if not t.org else f"{t.name} ({t.org})"
        section.lines.append(
            Line(
                text=(
                    f"{who}: {t.chip()}. "
                    f"Last: {t.source_row['source_title'] or 'no subject'}."
                ),
                provenance=SourceRef(
                    source=str(t.source_row["source"]),
                    external_id=str(t.source_row["source_external_id"]),
                    occurred_at=str(t.source_row["source_occurred_at"]),
                    title=t.source_row["source_title"],
                    source_item_id=int(t.source_row["source_item_id"]),
                ),
                status="slipping",
            )
        )
    return section


def checklist_section(conn: sqlite3.Connection, today: date, settings: Settings) -> Section:
    """docs/04 C5. "The checklist appears in the brief only if incomplete items remain."

    C4: a broken streak resets quietly. The streak count is shown when it is running and
    simply absent when it is not — there is no sentence about having lost one.
    """
    from backglass.goals import checklist as checklist_mod

    section = Section(priority=9, title="Checklist")
    for item in checklist_mod.incomplete(conn, settings, today):
        run = checklist_mod.streak(conn, settings, item.id, today)
        tail = f" ({run}d)" if run else ""
        section.lines.append(
            Line(
                text=f"{item.title}{tail}",
                provenance=LedgerRef("checklist", str(item.id), "checklist"),
            )
        )
    return section


def friend_plans_section(
    conn: sqlite3.Connection, today: date, settings: Settings
) -> Section:
    """docs/07 §Instagram. Plans made in DMs, deliberately last.

    Priority 11 puts this below everything, so under the B1 word cap it is the first
    section truncated — which is the owner's rule ("lower priority") expressed in the
    brief's own mechanics. Special events never reach here: `_demoted_friend_plan` is
    False for them, so they stay in Slipping/Awaiting at full priority and this section
    skips them to avoid saying the same thing twice.
    """
    section = Section(priority=11, title="Friend plans")
    horizon = today + timedelta(days=FRIEND_PLANS_HORIZON_DAYS)
    for row in _rows(
        conn,
        "brief_friend_plans",
        {
            "user_id": USER_ID,
            "horizon": horizon.isoformat(),
            "confidence_threshold": settings.confidence_threshold,
        },
    ):
        if not _demoted_friend_plan(row):
            continue  # special events ride the normal sections
        who = f" with {row['counterparty']}" if row["counterparty"] else ""
        section.lines.append(
            Line(
                text=f"{row['what']}{who}. {_due_phrase(row['due_at'], today)}.",
                provenance=_source_of(row),
                status=_status_of(row["due_at"], today),
                commitment_id=int(row["id"]),
            )
        )
    return section


def review_section(conn: sqlite3.Connection, today: date, settings: Settings) -> Section:
    """docs/05 §8. Guesses, rendered as questions.

    CLAUDE.md rule 2 says these never enter the brief "as fact". They enter it as a
    request for a decision, which is a different speech act, and design-system.md §4
    gives them a dashed keyline and no fill so that reads visually too.
    """
    section = Section(priority=10, title="Needs review")
    for row in _rows(
        conn,
        "brief_needs_review",
        {"user_id": USER_ID, "confidence_threshold": settings.confidence_threshold},
    ):
        who = f" — {row['counterparty']}" if row["counterparty"] else ""
        direction = "you owe" if row["direction"] == "i_owe" else "owed to you"
        section.lines.append(
            Line(
                text=(
                    f"Did {direction}: {row['what']}{who}? ({row['confidence']:.0%} confident)"
                ),
                provenance=_source_of(row),
                status="needs_review",
                commitment_id=int(row["id"]),
            )
        )
    return section


# ── assembly ──────────────────────────────────────────────────────────────


def build(conn: sqlite3.Connection, settings: Settings, for_date: date | None = None) -> Brief:
    """Assemble the brief. B3, B4 and B1 are applied here, in that order."""
    today = for_date or today_in(settings.default_tz)
    brief = Brief(generated_for_date=today.isoformat(), kind="daily")

    for section in (
        failure_section(conn, today, settings),
        timezone_section(conn, today, settings),
        plan_section(conn, today),
        capacity_section(conn, today),
        slipping_section(conn, today, settings),
        awaiting_section(conn, today, settings),
        goal_section(conn, today, settings),
        rollover_section(conn, today, settings),
        follow_up_section(conn, today, settings),
        checklist_section(conn, today, settings),
        review_section(conn, today, settings),
        friend_plans_section(conn, today, settings),
    ):
        brief.add(section)  # B3

    brief.deduplicate()  # B4
    brief.enforce_word_limit()  # B1
    brief.assert_provenance()  # B2

    # docs/04 §5: the drop-or-do question is asked exactly once. Marked only for the
    # items that actually survived truncation into the rendered brief.
    from backglass.plan import rollover as rollover_mod

    asked = [
        line.commitment_id
        for section in brief.sections
        if section.title == "Rolling over"
        for line in section.lines
        if line.commitment_id is not None
    ]
    if asked:
        rollover_mod.mark_question_asked(conn, asked)

    if not brief.sections:
        brief.notes.append(
            Note("Nothing open. No commitments due, none awaiting, none to review.")
        )
    return brief


def build_for(conn: sqlite3.Connection, settings: Settings, day: date) -> Brief:
    """The brief for `day`, whichever kind that is. docs/04 §2.7 W1 and W2.

    W1: "Monday planning replaces the brief; it does not arrive in addition to it. Two
    emails on Monday means neither is read." Enforced structurally — on a week-start day
    the daily sections are never built, so there is no code path that emits both.

    W2: the Friday retro appends to the normal brief and is trimmed to 150 words on its
    own before the 400-word ceiling is applied to the whole thing.
    """
    from backglass.brief import weekly

    if weekly.is_week_start(settings, day):
        return weekly.monday(conn, settings, day)

    brief = build(conn, settings, day)
    if weekly.is_week_end(settings, day):
        retro = weekly.friday(conn, settings, day)
        if not retro.empty:
            brief.kind = "friday"
            brief.add(retro)
            brief.deduplicate()
            brief.enforce_word_limit()
            brief.assert_provenance()
    return brief


def failure_brief(for_date: date, reason: str) -> Brief:
    """B6. "If generation fails, send a short failure notice rather than nothing."

    docs/05: "Silence is indistinguishable from 'no news' and that ambiguity is
    corrosive." The reason is deliberately terse and carries no ledger content — docs/08
    forbids body_text appearing in error reports, and an exception trace is an error
    report.
    """
    brief = Brief(generated_for_date=for_date.isoformat(), kind="daily")
    brief.notes.append(Note(f"Brief generation failed: {reason}. The ledger is unchanged."))
    brief.notes.append(Note("Open the dashboard for current state."))
    return brief


def persist(conn: sqlite3.Connection, brief: Brief, content_md: str) -> int:
    """Store the brief so B7 has something to record `opened_at` against.

    UPSERT on (user_id, generated_for_date, kind): regenerating a brief for the same day
    replaces it rather than failing, but deliberately does not clear `opened_at`, because
    the fact that the owner read that morning's brief remains true.
    """
    items = [
        {"text": line.text, "source": line.provenance.label, "status": line.status}
        for line in brief.all_lines()
    ]
    cursor = conn.execute(
        "INSERT INTO brief (user_id, generated_for_date, kind, content_md, items_json, "
        "                   word_count) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id, generated_for_date, kind) DO UPDATE SET "
        "  content_md = excluded.content_md, items_json = excluded.items_json, "
        "  word_count = excluded.word_count "
        "RETURNING id",
        (
            USER_ID,
            brief.generated_for_date,
            brief.kind,
            content_md,
            json.dumps(items),
            brief.word_count(),
        ),
    ).fetchone()
    return int(cursor["id"])
