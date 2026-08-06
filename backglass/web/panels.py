"""Reading the seven panels. docs/06 §Panels.

One function per panel, plus `everything()` which assembles them for a render. Kept apart
from routing so a panel can be tested without a client, and apart from `actions.py` so the
read and write halves of the dashboard cannot quietly grow into each other.

Every panel returns its own declarative empty state (docs/06 §Empty states). "Nothing
open." Never "You're all caught up! 🎉" — docs/11 §Cross-cutting rule 5 calls that a dark
pattern even when it is friendly.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from backglass import dedup
from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID

#: How far back the plan review queue looks; see `_review_floor`.
REVIEW_FLOOR_DAYS = 60

#: How many recent sync runs the error summary reads. At the scheduled half-hourly
#: cadence this is roughly the working day, which is the span the dashboard is opened
#: over. A run count rather than a wall-clock window on purpose: when the scheduler dies,
#: "the last 24 hours" empties out exactly when the ledger is most wrong, taking the
#: errors that explain the silence with it. See recent_run_errors.sql.
ERROR_WINDOW_RUNS = 20

#: How many grouped error lines the Sources panel prints before it says "N more".
#: Grouping is doing the compression work — the owner's whole 119-run history collapses
#: to four distinct causes — so this is a guard against a pathological run, not the
#: normal case. A tighter cap would hide a real cause behind an overflow count on a
#: perfectly ordinary morning. `backglass errors` prints the window uncapped.
ERROR_ROWS = 8


@dataclass
class Panel:
    title: str
    empty_text: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.rows


@dataclass
class Dashboard:
    today: date
    today_panel: Panel
    board: Panel
    awaiting: Panel
    goals: Panel
    checklist: Panel
    review: Panel
    sources: Panel
    lanes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _rows(conn: sqlite3.Connection, name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return conn.execute(query(name), params).fetchall()


def week_start_of(day: date, week_start: str = "monday") -> date:
    offset = day.weekday() if week_start.lower() == "monday" else (day.weekday() + 1) % 7
    return day - timedelta(days=offset)


def due_label(due_at: Any, today: date) -> str:
    """A due date in words, sized to its distance — the G13 principle applied to
    commitments: "due Fri" beats "due 2026-08-01" for anything inside a week, and
    the year appears exactly when it differs. Overdue/due-today rows never reach
    this; their state chips already carry the words."""
    if not due_at:
        return "no date"
    try:
        due = date.fromisoformat(str(due_at)[:10])
    except ValueError:
        return f"due {due_at}"
    if 0 <= (due - today).days <= 6:
        return f"due {due.strftime('%a')}"
    if due.year == today.year:
        return f"due {due.strftime('%d %b')}"
    return f"due {due.strftime('%d %b %Y')}"


def urgency_bucket(due_at: Any, today: date) -> str:
    """The board's section grammar (owner ruling 2026-08-05): a commitment's place on
    the page is when it bites — Overdue, Today, This week, Later, No date — not which
    way the promise points. Direction survives as an inline marker on the row."""
    if not due_at:
        return "No date"
    try:
        due = date.fromisoformat(str(due_at)[:10])
    except ValueError:
        return "No date"
    if due < today:
        return "Overdue"
    if due == today:
        return "Today"
    if (due - today).days <= 6:
        return "This week"
    return "Later"


def board_due_line(due_at: Any, today: date) -> str:
    """What a board row still says about its date once its bucket heading has spoken.

    Overdue rows carry the size of the slip; a Today row says nothing — the heading is
    the whole message; future rows keep due_label's distance-sized wording.
    """
    bucket = urgency_bucket(due_at, today)
    if bucket in ("No date", "Today"):
        return ""
    if bucket == "Overdue":
        days = (today - date.fromisoformat(str(due_at)[:10])).days
        return f"{days}d late"
    return due_label(due_at, today)


def when_label(starts_at: Any, today: date) -> str:
    """`due_label`'s wording for something that is not due.

    A plan has a date, not a deadline: "due Wed" reads as an obligation to hand
    something in, which is precisely the distinction the engagement record exists to
    draw. Same distance-sizing, same year rule, no "due".
    """
    if not starts_at:
        return "no date yet"
    try:
        when = date.fromisoformat(str(starts_at)[:10])
    except ValueError:
        return str(starts_at)
    if when == today:
        return "today"
    if when < today:
        return when.strftime("%d %b") if when.year == today.year else when.strftime("%d %b %Y")
    if (when - today).days <= 6:
        return when.strftime("%a")
    if when.year == today.year:
        return when.strftime("%d %b")
    return when.strftime("%d %b %Y")


def relative(timestamp: Any, now: datetime | None = None) -> str:
    """ "Relative timestamp of last successful sync" — docs/06 §The Sources panel."""
    if not timestamp:
        return "never"
    try:
        then = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    reference = now or datetime.now(tz=then.tzinfo)
    seconds = int((reference - then).total_seconds())
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


# ── the seven panels ──────────────────────────────────────────────────────


def today_panel(conn: sqlite3.Connection, today: date) -> Panel:
    rows = _rows(conn, "dashboard_today", {"user_id": USER_ID, "local_date": today.isoformat()})
    panel = Panel(
        title="Today",
        empty_text="No plan for today. The day planner runs at 5:45am.",
        rows=rows,
    )
    if rows:
        first = rows[0]
        panel.meta = {
            "capacity_minutes": first["capacity_minutes"],
            "planned_minutes": first["planned_minutes"],
            "overflow_count": first["overflow_count"],
            "tz": first["tz"],
            "has_protected": any(r["kind"] == "protected" for r in rows),
        }
    return panel


def board_panel(conn: sqlite3.Connection, settings: Settings, today: date) -> Panel:
    rows = _rows(
        conn,
        "dashboard_board",
        {
            "user_id": USER_ID,
            "today": today.isoformat(),
            "confidence_threshold": settings.confidence_threshold,
        },
    )
    for row in rows:
        row["bucket"] = urgency_bucket(row["due_at"], today)
        row["due_line"] = board_due_line(row["due_at"], today)
    # Long-overdue rows fold into Stale rather than crowding the lane a person scans
    # every morning. A 120-day mail backfill delivers months-old obligations that were
    # answered before the ledger existed; they are real extractions and real decisions
    # to make, but they are not today's news, and thirty of them drown the three that
    # are. The split is presentation only — same rows, same actions, still counted open.
    def _days_late(row: dict[str, Any]) -> int:
        return (today - date.fromisoformat(str(row["due_at"])[:10])).days

    live = [
        r for r in rows
        if r["bucket"] != "Overdue" or _days_late(r) <= settings.stale_after_days
    ]
    stale = [
        r for r in rows
        if r["bucket"] == "Overdue" and _days_late(r) > settings.stale_after_days
    ]
    return Panel(
        title="Commitments",
        empty_text="Nothing open.",
        rows=live,
        # The threshold rides in meta because the board also renders as an HTMX
        # fragment whose context has no `settings` — a template that reaches for it
        # there raises at the first swap, not at review time.
        meta={
            "stale": stale,
            "stale_after_days": settings.stale_after_days,
            "suspects": dedup.suspects(conn),
        },
    )


def swimlanes(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """docs/06: "Board grouped by status, swimlanes by counterparty."

    Insertion-ordered, and the board query sorts deterministically (urgency, then due
    date, then counterparty), so the lanes come out stable between renders. A board
    whose lanes reorder on every HTMX swap is a board you cannot build muscle memory
    against.
    """
    lanes: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        lanes.setdefault(str(row["counterparty"] or "No counterparty"), []).append(row)
    return lanes


def awaiting_panel(conn: sqlite3.Connection, settings: Settings, today: date) -> Panel:
    rows = _rows(
        conn,
        "brief_awaiting",
        {
            "user_id": USER_ID,
            "today": today.isoformat(),
            "confidence_threshold": settings.confidence_threshold,
        },
    )
    return Panel(title="Awaiting others", empty_text="Nothing outstanding.", rows=rows)


def goals_panel(conn: sqlite3.Connection, settings: Settings, today: date) -> Panel:
    from backglass.goals import health

    rows = _rows(
        conn,
        "dashboard_goals",
        {
            "user_id": USER_ID,
            "week_start": week_start_of(today, settings.week_start).isoformat(),
        },
    )
    targeted = [r for r in rows if r["target_id"] is not None]
    # The dashboard is the glance surface: cadences and anything that has moved
    # show as rows; untouched milestones collapse to one counted line. Their full
    # list lives on /goals — fourteen "no checkpoints yet" rows here was the
    # wallpaper the audit condemned, doubled by every started roadmap.
    quiet = [
        r for r in targeted
        if not r["weekly_count"] and not r["done_this_week"] and not r["last_checkpoint"]
    ]
    quiet_ids = {r["target_id"] for r in quiet}

    # Format-audit ruling: the dashboard panel is a per-goal summary, not a clone
    # of /goals. One line per goal — week aggregate across its cadence targets,
    # first lifetime total as the headline number — and no controls: logging and
    # cadence changes live on /goals, where the work happens.
    summary: list[dict[str, Any]] = []
    by_goal: dict[int, dict[str, Any]] = {}
    for r in targeted:
        g = by_goal.setdefault(
            int(r["goal_id"]),
            {"goal_id": int(r["goal_id"]), "title": r["goal_title"],
             "week_done": 0, "week_total": 0, "headline": None},
        )
        if r["weekly_count"]:
            g["week_done"] += int(r["done_this_week"] or 0)
            g["week_total"] += int(r["weekly_count"])
        elif r["kind"] == "total" and r["total_count"] and g["headline"] is None:
            g["headline"] = {
                "title": r["target_title"],
                "done": int(r["lifetime_done"] or 0),
                "total": int(r["total_count"]),
            }
    summary = list(by_goal.values())

    return Panel(
        title="Goals",
        # docs/06 §Empty states, verbatim. It is the sharpest line in the document and it
        # is doing real work: a goal with no target cannot be progressed against.
        empty_text="No targets set. A goal without a target is inert.",
        rows=[r for r in targeted if r["target_id"] not in quiet_ids],
        meta={
            "summary": summary,
            "quiet_milestones": len(quiet),
            "goals_without_targets": [r for r in rows if r["target_id"] is None],
            # docs/06 §Panels, Goals: "staleness chips, risk projections". Two separate
            # maps because G11 forbids merging the signals.
            "staleness": {s.goal_id: s for s in health.staleness(conn, settings, today)},
            "risk": {r.goal_id: r for r in health.risk(conn, settings, today)},
        },
    )


def checklist_panel(conn: sqlite3.Connection, today: date) -> Panel:
    #: specs/schema.sql: weekday_mask bit 0 = Monday.
    weekday_bit = 1 << today.weekday()
    rows = _rows(
        conn,
        "dashboard_checklist",
        {"user_id": USER_ID, "local_date": today.isoformat(), "weekday_bit": weekday_bit},
    )
    done = sum(1 for r in rows if r["tick_id"])
    return Panel(
        title="Checklist",
        empty_text="No checklist items for today.",
        rows=rows,
        meta={"done": done, "total": len(rows)},
    )


def review_panel(conn: sqlite3.Connection, settings: Settings) -> Panel:
    """Both record types, in one queue.

    Plans were missing here long after the brief had them, so the panel — and the
    "N extractions awaiting review" nudge counted off it — silently undercounted by
    every low-confidence plan in the ledger. `record` tells the template which shape it
    is holding and which endpoint its buttons post to; it is not `kind`, which the
    engagement rows already use for social/professional.
    """
    params = {"user_id": USER_ID, "confidence_threshold": settings.confidence_threshold}
    rows = [dict(row, record="commitment") for row in _rows(conn, "brief_needs_review", params)]
    rows += [
        dict(row, record="plan")
        for row in _rows(
            conn, "brief_needs_review_plans", {**params, "floor": _review_floor(settings)}
        )
    ]
    return Panel(title="Review queue", empty_text="Nothing to review.", rows=rows)


def _review_floor(settings: Settings) -> str:
    """How far back the plan queue looks.

    A guess about a plan that was supposed to happen last year is not a question worth
    asking every morning forever — the same unbounded-growth defect the Plans section
    itself had. Commitments need no equivalent because a commitment has no date it
    becomes moot on; a plan does.
    """
    from backglass.brief.daily import today_in

    return (today_in(settings.default_tz) - timedelta(days=REVIEW_FLOOR_DAYS)).isoformat()
#: The three sources `backglass auth` actually knows how to reconnect — the OAuth
#: providers. detect.py builds the same command for its NEEDS_SETUP hint.
OAUTH_KINDS = ("gmail", "calendar", "drive")


def auth_hint(source: str) -> str | None:
    """The command that repairs `source`, or None when there is no such command.

    Computed here rather than in the template because it is CLI knowledge, and because
    the string the template used to build — `backglass auth <last segment>` — is only
    right for gmail:<label>. For `anki` it printed `backglass auth anki`, which either
    exits complaining about GOOGLE_CLIENT_ID or stores a junk `gmail:anki` credential.
    """
    kind, _, label = source.partition(":")
    if kind in OAUTH_KINDS and label:
        return f"backglass auth {label} --source {kind}"
    return None


def _damage(row: dict[str, Any]) -> str:
    """How much of the ledger this failure cost, in the unit that is actually true.

    Items, not error records. sync.py re-attempts a parked item on every run, so a small
    persistent fault writes the same failure again every half hour: the owner's expired
    OAuth session produced forty error records for eleven items over four hours. Reading
    the record count as damage inflates it 3.6x here, and the inflation grows with how
    long the fault runs — the longer a small bug lasts, the larger the crisis the panel
    invents. A connector or spend-cap error has no item behind it at all, so it counts
    the only thing it has.
    """
    items = int(row["items"])
    if not items:
        count = int(row["occurrences"])
        return f"{count} time{'' if count == 1 else 's'}"
    return f"{items} item{'' if items == 1 else 's'}"


def error_line(row: dict[str, Any], now: datetime | None = None) -> str:
    """One grouped pipeline failure as a sentence, for the Sources panel.

    The damage leads, then the cause, then the reach. The stage is a verb here rather
    than a parenthetical because `triage 8547:` is where the error was caught, not what
    went wrong — and the item id it carried is exactly why forty records looked like
    forty problems. Retry volume trails as context and only when it differs from the
    damage, because "11 items · 40 attempts" is worth a reader's attention and
    "2 items · 2 attempts" is noise.
    """
    runs = int(row["run_count"])
    window = int(row["window_runs"])
    items, occurrences = int(row["items"]), int(row["occurrences"])
    if row["stage"]:
        subject = f"{_damage(row)} failed {row['stage']} · {row['message']}"
        # Retries are the same failure seen again, so they only earn a word when the
        # ledger cost and the record count actually diverge.
        extra = f", {occurrences} attempts" if occurrences != items else ""
    else:
        subject = str(row["message"])
        extra = f", {occurrences} times" if occurrences != runs else ""
    return (
        f"{subject} · {runs} of the last {window} sync{'' if window == 1 else 's'}"
        f"{extra} · {relative(row['last_at'], now)}"
    )


def error_alert(row: dict[str, Any]) -> str:
    """The same failure as a sidebar line.

    Shorter than the panel's: the alert is a pointer to the panel that carries the rest,
    so it spends its words on what broke, how much it cost, and that it is not a one-off.
    It still names its subject in full, because an alarm that does not is a mystery
    rather than a cue. The window is the one that was read, never the constant — on a
    four-run ledger with three bad runs, "3 of the last 20" renders a near-total outage
    as a 15% blip.
    """
    runs = int(row["run_count"])
    reach = f"in {runs} of the last {int(row['window_runs'])} syncs"
    if not int(row["items"]) and int(row["occurrences"]) == runs:
        # A connector that failed once per run has no second number worth printing.
        return f"{row['message']} — {reach}"
    return f"{row['message']} — {_damage(row)}, {reach}"


def sources_panel(conn: sqlite3.Connection, settings: Settings) -> Panel:
    from backglass.connectors import detect

    rows = _rows(conn, "dashboard_sources", {"user_id": USER_ID})
    for row in rows:
        row["fix"] = auth_hint(str(row["source"]))
    kill = conn.execute(query("triage_kill_rate"), {"user_id": USER_ID}).fetchone()
    total = int((kill or {}).get("total") or 0)
    rate = float((kill or {}).get("kill_rate") or 0.0)
    last = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()

    # Stores sitting on this machine that one `backglass setup` run would hook up.
    # Detection is stat-calls only, cheap enough for every render (Phase A2).
    authed = {r["source"] for r in rows if r["status"] == "ok"}
    configured = {r["source"] for r in rows}
    found = [
        {"source": d.source, "hint": d.hint}
        for d in detect.detect_all(settings, authed=authed)
        if d.status == detect.FOUND and d.source not in configured
    ]

    degraded = bool(last and last["degraded"])

    # What the pipeline recorded as gone wrong lately. CLAUDE.md rule 5 says a failing
    # source degrades and is surfaced in the Sources panel; until this existed the second
    # half of that sentence was not true anywhere in the web layer, and
    # specs/extraction-prompts/extract-commitments.md §Failure handling was promising a
    # dashboard surface that did not exist.
    errors = _rows(conn, "recent_run_errors", {"user_id": USER_ID, "runs": ERROR_WINDOW_RUNS})

    return Panel(
        title="Sources",
        empty_text="No sources configured. Run `backglass setup`.",
        rows=rows,
        meta={
            "found": found,
            # Evidence with no connector behind it: quick-adds, imports, a source whose
            # credential row is gone. It cites into the brief like anything else, so it
            # is named here rather than left invisible. See unmanaged_sources.sql.
            "unmanaged": _rows(conn, "unmanaged_sources", {"user_id": USER_ID}),
            "kill_rate": rate,
            "triaged": total,
            # docs/06: "If it drops below 85 percent the rules have drifted and cost is
            # about to climb." Computed here so the template does not carry a threshold.
            "kill_rate_low": total > 0 and rate < 0.85,
            # A paused source is not a failing one — the owner chose the silence.
            "any_failed": any(r["status"] != "ok" and r["enabled"] for r in rows),
            "last_run": last,
            "degraded": degraded,
            # One sentence, built once, read by the panel and by the sidebar alert, so
            # the two surfaces cannot disagree about how bad the pause is. Only computed
            # when it holds — it costs a prompt-file read and a count.
            "degraded_note": (
                degraded_note(conn, reason=_reason_of(last)) if degraded else None
            ),
            # Capped like the sidebar's alerts, and counted rather than dropped. A run
            # with nothing wrong yields an empty list and the template prints nothing at
            # all: an empty error block is worse than no block, because it trains the eye
            # to skip the place the real alarm will appear.
            "errors": [dict(row, line=error_line(row)) for row in errors[:ERROR_ROWS]],
            "more_errors": max(0, len(errors) - ERROR_ROWS),
            # The window that was actually read, not the constant that asked for it.
            "error_window": int(errors[0]["window_runs"]) if errors else 0,
            # Recent AND repeating: still present in the newest run, and seen in more
            # than one. A single flare is shown in the panel but does not raise the
            # sidebar — the alert exists for the failure that is not going to fix itself,
            # and one that cries wolf on every transient is one the owner learns to skip.
            "errors_now": [
                row for row in errors if row["in_latest_run"] and int(row["run_count"]) > 1
            ],
        },
    )


def _reason_of(run: sqlite3.Row | None) -> str | None:
    return str(run["degrade_reason"]) if run and run["degrade_reason"] else None


def degraded_note(
    conn: sqlite3.Connection, today: date | None = None, *, reason: str | None = None
) -> str:
    """What "extraction paused" actually means today, in items and in dates.

    The old line stopped at "triage only", which told the owner the cap had been reached
    and nothing about the two facts that decide what to do next: how much of the ledger
    is missing, and how long it stays missing. The cap resets on the calendar month and
    has no other release, so a pause on the 3rd is a four-week pause.

    Two pauses now exist and they have opposite shapes, so `reason` (run.degrade_reason,
    migration 0016) picks the sentence. A usage window clears on its own within hours, has
    no reset-of-the-month date, and leaves its items pending rather than parked — telling
    it in the cap's words would hand the owner a four-week date for a lunchtime pause, and
    name a cap that was never reached. `None` is a pre-0016 row, where degraded could only
    have meant the cap.

    A rate limit carries which stage it stopped ('rate_limit:triage' | 'rate_limit:extract'
    — sync.SyncReport.rate_limited_stage), because that decides both halves of the
    sentence. Only the extraction case is "extraction paused, triage only": that is the
    cap's shape, and it is true there. A limit hit during triage did the opposite — triage
    stopped and extraction never ran at all — and its items have no verdict, so the
    stranded count, which opens with `triage_verdict = 'keep'`, is structurally zero for
    it. Borrowing the cap's sentence there asserted a stage that did not run and printed
    "0 items waiting" at the moment the most of the ledger was missing.

    Anything the split does not recognise — 'spend_cap', NULL, a value from a future
    version — falls through to the cap, which is the behaviour every one of them had.
    """
    from backglass import costs

    kind, _, stage = (reason or "").partition(":")
    if kind == "rate_limit":
        return _rate_limit_note(conn, stage)
    stranded = costs.stranded_extractions(conn)
    waiting = f"{stranded} item{'' if stranded == 1 else 's'} waiting"
    resets = costs.cap_resets_on(today)
    return (
        "Spend cap reached — extraction paused, triage only. "
        f"{waiting}; the cap resets {resets.day} {resets:%b}."
    )


def _rate_limit_note(conn: sqlite3.Connection, stage: str) -> str:
    """One sentence per stage, each counting the population that stage stranded."""
    from backglass import costs

    retried = "they were left pending, and the next scheduled sync retries them."
    if stage == "triage":
        unread = costs.untriaged_items(conn)
        return (
            "Model rate limit reached — the run stopped while reading its new items, "
            f"and extraction did not run. {unread} item{'' if unread == 1 else 's'} "
            f"unread; {retried}"
        )
    if stage == "extract":
        stranded = costs.stranded_extractions(conn)
        return (
            "Model rate limit reached — extraction paused for that run, triage only. "
            f"{stranded} item{'' if stranded == 1 else 's'} waiting; {retried}"
        )
    # A row written before the stage was recorded. Which population is missing is not
    # knowable from it, and a count taken from the wrong one is worse than no count.
    return (
        "Model rate limit reached — the run stopped early, and the items it did not "
        "reach were left pending; the next scheduled sync retries them."
    )


# ── the sidebar ───────────────────────────────────────────────────────────


@dataclass
class Sidebar:
    """At-a-glance state for the shell sidebar, shared by every page.

    Alerts are derived from live state on every read, never stored — an alert that
    appears when a condition holds and vanishes when it stops needs no dismissal
    machinery, no seen-flags, and cannot violate idempotency. docs/11 §3 bans toasts;
    this is the non-toast shape of "in-app notification".
    """

    alerts: list[dict[str, str]] = field(default_factory=list)
    goals: list[dict[str, Any]] = field(default_factory=list)
    roadmaps: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    #: Alerts beyond the display cap — shown as "N more" rather than dropped silently.
    more_alerts: int = 0


def sidebar(
    conn: sqlite3.Connection,
    settings: Settings,
    today: date,
    now: datetime | None = None,
) -> Sidebar:
    from backglass import heartbeat as heartbeat_mod
    from backglass.goals import health
    from backglass.people import touch
    from backglass.web.routes.roadmaps import list_roadmaps

    board = board_panel(conn, settings, today)
    review = review_panel(conn, settings)
    sources = sources_panel(conn, settings)

    staleness = {s.goal_id: s for s in health.staleness(conn, settings, today)}
    risks = {r.goal_id: r for r in health.risk(conn, settings, today)}

    def risk_line(goal_id: int, title: str) -> str | None:
        # G13's sentence leads with the goal title; the sidebar line sits under a link
        # that already names the goal, so the prefix would read as a stutter.
        sentence = risks[goal_id].sentence() if goal_id in risks else None
        return sentence.removeprefix(f"{title}: ") if sentence else None

    goals = [
        {
            "goal_id": goal_id,
            "title": s.goal_title,
            "staleness_chip": s.chip(),
            "staleness_level": s.ink_level,
            "at_risk": bool(risks.get(goal_id) and risks[goal_id].at_risk),
            "risk_sentence": risk_line(goal_id, s.goal_title),
        }
        for goal_id, s in staleness.items()
    ]

    roadmaps = [
        {
            "id": r["id"],
            "title": r["title"],
            "done_steps": int(r["done_steps"]),
            "live_steps": int(r["live_steps"]),
        }
        for r in list_roadmaps(conn)
        if r["status"] == "active"
    ]

    # An ALERT is abnormal AND actionable AND names its subject. Goal risk is
    # status, not an alert — it lives in the GOALS block below (and on /goals),
    # so sustained_risk no longer duplicates itself here. Format-audit ruling.
    alerts: list[dict[str, str]] = []

    # First, because a dead scheduler makes every other panel a confident lie: they are
    # all reporting the ledger accurately, and the ledger stopped moving. docs/11 §8.
    beat = heartbeat_mod.read(conn, settings, today, now)
    if beat.never_ran:
        alerts.append(
            {"level": "verm", "text": "Sync has never run — nothing here is from your mail",
             "href": "/#panel-sources"}
        )
    elif beat.stale:
        alerts.append(
            {"level": "verm",
             "text": f"Last sync ran {beat.age_phrase} — scheduled jobs may be dead",
             "href": "/#panel-sources"}
        )
    if beat.plan_missing:
        alerts.append(
            {"level": "gold", "text": "No plan for today — the 5:45am planner did not run",
             "href": "/schedule"}
        )

    for s in sources.rows:
        if s["status"] != "ok" and s["enabled"]:
            # docs/11 §Cross-cutting rule 4: failures are louder than successes —
            # but an alarm that does not name its subject is a mystery, not a cue.
            alerts.append(
                {"level": "verm", "text": f"{s['source']} is failing — views are incomplete",
                 "href": "/#panel-sources"}
            )
    # Louder than the spend-cap note below it and placed above it deliberately: a pause
    # the owner configured is a known cost, and a run that keeps failing is not. This is
    # the alert that was missing on 2026-08-05, when eight consecutive syncs died on an
    # expired OAuth session and every surface stayed green.
    for row in sources.meta["errors_now"]:
        alerts.append(
            {"level": "verm", "text": error_alert(row), "href": "/#panel-sources"}
        )
    if sources.meta["degraded"]:
        alerts.append(
            {"level": "gold", "text": sources.meta["degraded_note"],
             "href": "/#panel-sources"}
        )
    if sources.meta["kill_rate_low"]:
        alerts.append(
            {"level": "gold", "text": "Triage kill rate below 85% — rules have drifted",
             "href": "/#panel-sources"}
        )
    from backglass import chats as chats_mod

    waiting = chats_mod.undecided(conn)
    if waiting:
        n = len(waiting)
        alerts.append(
            {"level": "dash",
             "text": f"{n} new conversation{'s' if n != 1 else ''} to monitor or ignore",
             "href": "/chats"}
        )

    if review.rows:
        n = len(review.rows)
        alerts.append(
            {"level": "dash",
             "text": f"{n} extraction{'s' if n != 1 else ''} awaiting review",
             "href": "/#panel-review"}
        )

    # Cap the block so a bad morning cannot push the goals out of the sidebar.
    cap = 4
    more = max(0, len(alerts) - cap)

    return Sidebar(
        alerts=alerts[:cap],
        goals=goals,
        roadmaps=roadmaps,
        counts={
            "open": len(board.rows),
            "follow_ups": len(touch.needing_follow_up(conn, settings, today)),
            "roadmaps": len(roadmaps),
            "new_chats": len(waiting),
        },
        more_alerts=more,
    )


def everything(conn: sqlite3.Connection, settings: Settings, today: date) -> Dashboard:
    board = board_panel(conn, settings, today)
    return Dashboard(
        today=today,
        today_panel=today_panel(conn, today),
        board=board,
        awaiting=awaiting_panel(conn, settings, today),
        goals=goals_panel(conn, settings, today),
        checklist=checklist_panel(conn, today),
        review=review_panel(conn, settings),
        sources=sources_panel(conn, settings),
        lanes=swimlanes(board.rows),
    )
