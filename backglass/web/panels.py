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

from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID


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
        empty_text="No plan for today. The day planner runs at 05:45.",
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
    return Panel(title="Commitments", empty_text="Nothing open.", rows=rows)


def swimlanes(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """docs/06: "Board grouped by status, swimlanes by counterparty."

    Insertion-ordered, and the board query already sorts by direction then counterparty,
    so the lanes come out stable between renders. A board whose lanes reorder on every
    HTMX swap is a board you cannot build muscle memory against.
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
    return Panel(
        title="Goals",
        # docs/06 §Empty states, verbatim. It is the sharpest line in the document and it
        # is doing real work: a goal with no target cannot be progressed against.
        empty_text="No targets set. A goal without a target is inert.",
        rows=[r for r in rows if r["target_id"] is not None],
        meta={
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
    rows = _rows(
        conn,
        "brief_needs_review",
        {"user_id": USER_ID, "confidence_threshold": settings.confidence_threshold},
    )
    return Panel(title="Review queue", empty_text="Nothing to review.", rows=rows)


def sources_panel(conn: sqlite3.Connection, settings: Settings) -> Panel:
    from backglass.connectors import detect

    rows = _rows(conn, "dashboard_sources", {"user_id": USER_ID})
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

    return Panel(
        title="Sources",
        empty_text="No sources configured. Run `backglass setup`.",
        rows=rows,
        meta={
            "found": found,
            "kill_rate": rate,
            "triaged": total,
            # docs/06: "If it drops below 85 percent the rules have drifted and cost is
            # about to climb." Computed here so the template does not carry a threshold.
            "kill_rate_low": total > 0 and rate < 0.85,
            # A paused source is not a failing one — the owner chose the silence.
            "any_failed": any(r["status"] != "ok" and r["enabled"] for r in rows),
            "last_run": last,
            "degraded": bool(last and last["degraded"]),
        },
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


def sidebar(conn: sqlite3.Connection, settings: Settings, today: date) -> Sidebar:
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
            "staleness_level": s.level,
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

    alerts: list[dict[str, str]] = []
    if sources.meta["any_failed"]:
        alerts.append(
            # docs/11 §Cross-cutting rule 4: failures are louder than successes.
            {"level": "verm", "text": "A source is failing — views are incomplete",
             "href": "/#panel-sources"}
        )
    if sources.meta["degraded"]:
        alerts.append(
            {"level": "gold", "text": "Spend cap reached — extraction paused, triage only",
             "href": "/#panel-sources"}
        )
    if sources.meta["kill_rate_low"]:
        alerts.append(
            {"level": "gold", "text": "Triage kill rate below 85% — rules have drifted",
             "href": "/#panel-sources"}
        )
    for r in health.sustained_risk(conn, settings, today):
        alerts.append(
            {"level": "gold", "text": f"{r.goal_title}: at risk two weeks running",
             "href": "/#panel-goals"}
        )
    if review.rows:
        n = len(review.rows)
        alerts.append(
            {"level": "dash",
             "text": f"{n} extraction{'s' if n != 1 else ''} awaiting review",
             "href": "/#panel-review"}
        )

    return Sidebar(
        alerts=alerts,
        goals=goals,
        roadmaps=roadmaps,
        counts={
            "open": len(board.rows),
            "follow_ups": len(touch.needing_follow_up(conn, settings, today)),
            "roadmaps": len(roadmaps),
        },
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
