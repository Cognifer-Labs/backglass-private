"""Notifications: the system speaks first, inside the hours the owner allows.

Until now every surface waited to be opened — the brief at 06:00, the dashboard when
the owner thought of it, /ask never unless visited. The goal (tasks/todo.md,
2026-08-18) asks for the opposite motion: when the ledger knows something the owner
should hear today, say so. This module is that mouth, and it is deliberately small:

  - **Deciders are reports over the ledger**, same doctrine as context.py: what is due
    today, what waits on an answer. Nothing here forms a belief; it reads typed
    records and states counts. Other modules (the replanner) may also hand it a
    ready-made notification through `record`.
  - **Once per (kind, subject, local day)** — rule 3 enforced by the notification
    table's UNIQUE key, not by caller discipline. Two syncs in one morning produce
    one banner; the second INSERT OR IGNORE writes nothing and stays silent.
  - **The row is the record, delivery is best-effort.** osascript can fail (no GUI
    session, a display asleep); the ledger still answers "what did the system tell
    the owner and when", which Notification Center's memory cannot.
  - **Quiet hours use the owed-at pattern** (catchup.py): outside `notify_window`
    nothing is delivered and nothing is recorded — before the window opens the
    banner is not yet owed, so a 06:00 sync leaves it for the 08:30 one instead of
    burning the day's dedup slot on a banner nobody saw.

Timezone note: "today" and "now" are the ACTIVE zone's (timezones.local_now), the
same clock the planner runs on, because a banner about "due today" that disagrees
with the plan about which day it is would be worse than silence.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

from backglass.config import Settings
from backglass.ledger import USER_ID

#: How many item names a digest line spells out; counts carry the rest.
NAME_LIMIT = 3


@dataclass(frozen=True)
class Sent:
    kind: str
    title: str
    body: str
    delivered: str


def run(
    conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None
) -> list[Sent]:
    """Deciders + delivery. Returns what was actually recorded this call.

    Failures degrade per decider (rule 5): one decider down loses one kind of
    notification, not the surface.
    """
    from backglass.plan import timezones

    if now is None:
        now = timezones.local_now(settings)
    if not _window_open(settings, now):
        return []

    sent: list[Sent] = []
    for decider in (_due_today, _questions_waiting, _tomorrow_prep):
        try:
            candidate = decider(conn, now)
        except Exception:  # noqa: BLE001 — rule 5: degrade, never block
            continue
        if candidate is None:
            continue
        kind, subject_key, title, body = candidate
        result = record(conn, settings, kind=kind, subject_key=subject_key,
                        title=title, body=body, now=now)
        if result is not None:
            sent.append(result)
    return sent


def record(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    kind: str,
    subject_key: str,
    title: str,
    body: str,
    now: datetime | None = None,
) -> Sent | None:
    """One notification through the dedup key, delivered if it is new.

    The door other modules use directly (the replanner's plan-replaced). Returns None
    when today's slot for (kind, subject_key) is already taken — which is the normal
    second-sync case, not an error. Outside the notify window nothing happens at all;
    scheduled deciders re-offer next sync, so the banner arrives when the window
    opens instead of never.
    """
    from backglass.plan import timezones

    if now is None:
        now = timezones.local_now(settings)
    if not _window_open(settings, now):
        return None

    cur = conn.execute(
        "INSERT OR IGNORE INTO notification"
        " (user_id, kind, subject_key, local_date, title, body, delivered, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        # From `now`, not the wall clock. `_stale_questions` compares a question's
        # `asked_at` against this column and its docstring claims "one clock against
        # itself" — which it was not: the row's `local_date` came from the caller's clock
        # and its `created_at` from `datetime.now()`, so the two disagreed for anything
        # that injects a time. In production they are the same instant and nothing
        # changes; under an injected clock the comparison becomes the one the docstring
        # describes, which is also why the re-banner rule could not be tested honestly.
        (
            USER_ID,
            kind,
            subject_key,
            now.date().isoformat(),
            title,
            body,
            now.astimezone(UTC).replace(microsecond=0).isoformat(),
        ),
    )
    if not cur.rowcount:
        return None  # today's slot already taken — rule 3, by schema
    row_id = int(cur.lastrowid or 0)
    delivered = _deliver(title, body)
    conn.execute("UPDATE notification SET delivered = ? WHERE id = ?", (delivered, row_id))
    return Sent(kind=kind, title=title, body=body, delivered=delivered)


def recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notification WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (USER_ID, limit),
    ).fetchall()


# ── deciders ───────────────────────────────────────────────────────────────


def _due_today(
    conn: sqlite3.Connection, now: datetime
) -> tuple[str, str, str, str] | None:
    """What the day demands: open commitments due today or overdue, one digest.

    substr(due_at, 1, 10) as everywhere — the column mixes bare dates and offset
    datetimes, and only the leading ten characters mean the local day under both.
    """
    today = now.date().isoformat()
    rows = conn.execute(
        """
        SELECT what, substr(due_at, 1, 10) AS due_day
        FROM commitment
        WHERE user_id = ? AND status = 'open'
          AND due_at IS NOT NULL AND substr(due_at, 1, 10) <= ?
        ORDER BY substr(due_at, 1, 10) DESC, id ASC
        """,
        (USER_ID, today),
    ).fetchall()
    due_today = [r for r in rows if r["due_day"] == today]
    if not due_today:
        return None  # a day with nothing due is a day with no banner
    overdue = len(rows) - len(due_today)
    names = "; ".join(str(r["what"]) for r in due_today[:NAME_LIMIT])
    more = f" (+{len(due_today) - NAME_LIMIT} more)" if len(due_today) > NAME_LIMIT else ""
    tail = f" · {overdue} older overdue" if overdue else ""
    return (
        "overdue-today",
        "digest",
        f"Due today: {len(due_today)}",
        f"{names}{more}{tail}",
    )


def _questions_waiting(
    conn: sqlite3.Connection, now: datetime
) -> tuple[str, str, str, str] | None:
    """Questions the system is holding instead of guessing — the owner unblocks it.

    Only when something was ASKED since the last banner. A queue the owner has seen
    and is sitting on must not re-banner every morning — a notification that fires
    every day is one that gets turned off, and with eleven long-lived questions on
    the live ledger "n > 0" would have been exactly that. Both timestamps come from
    now_iso(), so the comparison is one clock against itself.
    """
    del now
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(asked_at) AS newest FROM open_question"
        " WHERE user_id = ? AND status = 'open'",
        (USER_ID,),
    ).fetchone()
    n = int(row["n"] or 0)
    if not n:
        return None
    last = conn.execute(
        "SELECT created_at FROM notification WHERE user_id = ?"
        " AND kind = 'questions-waiting' ORDER BY id DESC LIMIT 1",
        (USER_ID,),
    ).fetchone()
    if last is not None and str(row["newest"] or "") <= str(last["created_at"]):
        return None  # nothing new since the owner was last told
    return (
        "questions-waiting",
        "digest",
        f"{n} question(s) waiting",
        "Backglass is planning around them until you answer — /ask",
    )


def _tomorrow_prep(
    conn: sqlite3.Connection, now: datetime
) -> tuple[str, str, str, str] | None:
    """What tomorrow holds, said today — preparing is a today activity.

    An 8am exam tomorrow is decided this evening: the banner exists so the owner hears
    it while there is still a today to prepare in, not at 7:40 the next morning. Same
    lines the plan's own "Tomorrow holds" note carries, so the two surfaces cannot
    disagree about what is coming.
    """
    from backglass.plan import planner

    lines = planner.tomorrow_preview(conn, now.date())
    if not lines:
        return None
    shown = "; ".join(lines[:NAME_LIMIT])
    more = f" (+{len(lines) - NAME_LIMIT} more)" if len(lines) > NAME_LIMIT else ""
    return (
        "tomorrow-prep",
        "digest",
        f"Tomorrow: {len(lines)} thing(s) to be ready for",
        f"{shown}{more}",
    )


# ── mechanics ──────────────────────────────────────────────────────────────


def _window_open(settings: Settings, now: datetime) -> bool:
    from backglass.plan import timezones

    start, end = timezones.parse_window(settings.notify_window)
    return start <= now.time() < end


def _deliver(title: str, body: str) -> str:
    """A macOS banner via osascript, best-effort. The row already exists either way.

    Arguments ride through `argv` into `on run argv`, never interpolated into the
    script source — a title containing a quote must not become AppleScript.
    """
    script = (
        'on run argv\n'
        'display notification (item 2 of argv) with title (item 1 of argv)\n'
        'end run'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script, title, body],
            capture_output=True, timeout=10, check=False, text=True,
        )
    except Exception as exc:  # noqa: BLE001 — no GUI, no osascript: same answer
        return f"failed: {type(exc).__name__}"
    if proc.returncode != 0:
        return f"failed: {(proc.stderr or '').strip()[:120] or 'osascript non-zero'}"
    return "osascript"
