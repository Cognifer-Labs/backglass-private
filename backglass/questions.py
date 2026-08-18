"""What Backglass cannot settle from the evidence, asked instead of guessed.

The ledger already has two places where the system defers to the owner: the review queue
(is this a real commitment?) and the dedup queue (are these two the same promise?). Both
ask about one record's confidence, in a fixed accept/reject shape.

A confusion is often not that shape. Two classes at the same hour are each perfectly
confident and cannot both be attended. An untitled calendar event is not a doubtful
commitment, it is an hour of the day nobody can account for. Two entities may be one
person. A fact may be asserted twice, differently. Each of those silently degrades the
plan — a smaller day, a split ledger, an hour spent on a placeholder — and nothing
anywhere says so.

Three rules, all inherited from surfaces that already work:

- **Ask once.** Identity is `(kind, subject_key)`, so a detector re-running finds its own
  question rather than raising it again every sync.
- **Never guess in the meantime.** A question does not change the ledger. Detection is
  read-only; only an answer writes, and it writes through `decisions` or `facts` where the
  provenance rules already live.
- **Always leave room for the owner's own words.** The options a detector enumerates are
  the cases it thought of. `answer_text` is free and is never secondary to the buttons —
  the fifth case the owner knows about is usually the true one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID

#: How far ahead the detectors look. The planner's horizon is the day; these are about
#: the days the owner can still do something about, and a conflict three months out is
#: not yet a question — the schedule will have changed twice by then.
HORIZON_DAYS = 21

#: Titles Calendar.app and its kin write when something is created and never named. An
#: hour called this is an hour nobody can account for, and it spends capacity exactly
#: like a real obligation.
PLACEHOLDER_TITLES = frozenset({"new event", "event", "untitled", "busy", "(no title)"})


@dataclass(frozen=True)
class Question:
    kind: str
    subject_key: str
    question: str
    detail: str
    options: list[str]

    def as_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject_key": self.subject_key,
            "question": self.question,
            "detail": self.detail,
            "options_json": json.dumps(self.options),
        }


# ── detectors ──────────────────────────────────────────────────────────────
#
# Each returns Questions and touches nothing. They are separate functions rather than one
# pass because they read different tables and fail independently: a detector that raises
# is one question missing, per rule 5, not a silent morning.


def _conflicts(conn: sqlite3.Connection, settings: Settings, today: date) -> list[Question]:
    """Two things the owner must be at, at the same time.

    The single most actionable thing a day organizer can say, and it currently says
    nothing. Live on the owner's store: LIA 101 10:10–11:00 against BIO 181 10:30–11:45
    on 2026-08-24. Stale data, a dropped section, or a real registration problem — the
    ledger cannot tell, and the difference matters before the 20th.

    Routines are excluded on purpose. Life bends around a class; a class does not bend
    around dinner, and asking about every meal that overlaps a lab would bury the
    conflicts that matter.
    """
    from backglass.plan import capacity

    #: Keyed by the pair of titles, not the pair plus a date. A class timetable that
    #: collides once collides every week, and asking about LIA 101 against BIO 181 on the
    #: 24th, the 31st and the 7th is asking one question three times.
    pairs: dict[tuple[str, str], tuple[list[date], str]] = {}
    for offset in range(HORIZON_DAYS):
        day = today + timedelta(days=offset)
        events = [
            e
            for e in capacity.day_events(conn, settings, day)
            if e.kind == "fixed"
            and not e.allday
            # A placeholder has its own question, and a better one. "Is CIS 236 or
            # New Event real?" invites an answer about the wrong thing — the owner
            # cannot say which of two events to attend when one of them is a name
            # Calendar.app invented.
            and e.title.strip().casefold() not in PLACEHOLDER_TITLES
        ]
        events.sort(key=lambda e: e.starts_at)
        for i, a in enumerate(events):
            for b in events[i + 1 :]:
                if b.starts_at >= a.ends_at:
                    break
                if a.title == b.title:
                    continue  # one event, described twice; `_collapse`'s problem
                key = (a.title, b.title) if a.title < b.title else (b.title, a.title)
                when = (
                    f"{a.title} — {a.starts_at:%H:%M}–{a.ends_at:%H:%M}\n"
                    f"{b.title} — {b.starts_at:%H:%M}–{b.ends_at:%H:%M}"
                )
                days, _ = pairs.setdefault(key, ([], when))
                days.append(day)

    out: list[Question] = []
    for (first, second), (days, when) in pairs.items():
        every = (
            f"on {days[0]:%a %d %b}"
            if len(days) == 1
            else f"on {len(days)} days, starting {days[0]:%a %d %b}"
        )
        out.append(
            Question(
                kind="conflict",
                subject_key=f"{first}|{second}",
                question=f"Two things at once {every}. Which is real?",
                detail=f"{when}\n\nYou cannot be at both. Stale timetable, a dropped "
                "section, or a registration clash — the ledger cannot tell which.",
                options=[
                    f"Attending {first}",
                    f"Attending {second}",
                    "Both — they do not really conflict",
                    "Neither is right",
                ],
            )
        )
    return out


def _untitled(conn: sqlite3.Connection, settings: Settings, today: date) -> list[Question]:
    """An hour with no name still costs an hour.

    `New Event` 09:00–10:00 subtracts from capacity like any obligation and renders on the
    schedule as one. It is either something the owner meant to name or a leftover from a
    calendar they tapped by accident, and only they know which.

    Grouped by title and time of day rather than asked per date, because the one on the
    owner's calendar repeats: nine days of "what is New Event at nine?" is nine ways of
    asking the same thing, and a surface that does that is one nobody opens twice. The
    count of days goes in the question instead, where it is the reason to care.
    """
    from backglass.plan import capacity

    seen: dict[tuple[str, str, int], list[date]] = {}
    for offset in range(HORIZON_DAYS):
        day = today + timedelta(days=offset)
        for event in capacity.day_events(conn, settings, day):
            if event.kind != "fixed" or event.allday:
                continue
            if event.title.strip().casefold() not in PLACEHOLDER_TITLES:
                continue
            minutes = int((event.ends_at - event.starts_at).total_seconds() // 60)
            seen.setdefault((event.title, f"{event.starts_at:%H:%M}", minutes), []).append(day)

    out: list[Question] = []
    for (title, at_time, minutes), days in seen.items():
        when = (
            f"on {days[0]:%a %d %b}"
            if len(days) == 1
            else f"on {len(days)} days, starting {days[0]:%a %d %b}"
        )
        out.append(
            Question(
                kind="untitled",
                subject_key=f"{title}|{at_time}|{minutes}",
                question=f"“{title}” takes {minutes} minutes at {at_time}, {when}. What is it?",
                detail=(
                    f"{minutes} minutes from {at_time}, {when}.\n"
                    f"That is {minutes * len(days)} minutes of capacity spent on something "
                    "with no name, and it shows on the schedule as a real obligation."
                ),
                options=[
                    "Real — I will name it in my calendar",
                    "Junk — ignore it from now on",
                ],
            )
        )
    return out


def _duplicate_entities(conn: sqlite3.Connection) -> list[Question]:
    """One person under two names splits everything counted per person.

    Surfaced by the dedup queue's counterparty line rather than looked for: "complete
    Dreamscape waiver online" is owed to both "Nyasha" and "Mrs. Shepard". Asking here is
    cheaper than a name-similarity pass and far more accurate, because the evidence is
    that two entities are attached to what looks like one promise.
    """
    rows = conn.execute(
        "SELECT a.what AS what, ea.id AS a_id, ea.canonical_name AS a_name,"
        "       eb.id AS b_id, eb.canonical_name AS b_name"
        " FROM commitment a"
        " JOIN commitment b ON b.what = a.what AND b.id > a.id AND b.direction = a.direction"
        " JOIN entity ea ON ea.id = a.counterparty_entity_id"
        " JOIN entity eb ON eb.id = b.counterparty_entity_id"
        " WHERE a.user_id = ? AND a.status = 'open' AND b.status = 'open'"
        "   AND a.counterparty_entity_id <> b.counterparty_entity_id"
        "   AND a.source_item_id <> b.source_item_id",
        (USER_ID,),
    ).fetchall()

    out: list[Question] = []
    for row in rows:
        pair = sorted((int(row["a_id"]), int(row["b_id"])))
        out.append(
            Question(
                kind="duplicate_entity",
                subject_key="|".join(str(p) for p in pair),
                question=f"Are {row['a_name']} and {row['b_name']} the same person?",
                detail=(
                    f"Both are owed “{row['what']}”, from different messages.\n"
                    "If they are one person, everything counted per person is currently split."
                ),
                options=[
                    f"Same person — keep {row['a_name']}",
                    f"Same person — keep {row['b_name']}",
                    "Different people",
                ],
            )
        )
    return out


def _contradictions(conn: sqlite3.Connection) -> list[Question]:
    """One thing asserted two ways, where the ledger keeps both.

    `fact` supersedes on write, so the live row is whichever landed last — which is a
    reasonable default and a bad one to be silent about when the two disagree and the
    older one had better evidence. The move-in date was Aug 5 and then Aug 9 across
    sources; the ledger held both and nothing asked which was true.
    """
    rows = conn.execute(
        "SELECT subject, key, COUNT(DISTINCT value) AS n,"
        "       GROUP_CONCAT(DISTINCT value) AS values_seen"
        " FROM fact WHERE user_id = ? AND superseded_by IS NOT NULL"
        " GROUP BY subject, key HAVING n > 1",
        (USER_ID,),
    ).fetchall()

    out: list[Question] = []
    for row in rows:
        values = [v.strip() for v in str(row["values_seen"]).split(",") if v.strip()][:4]
        if len(values) < 2:
            continue
        out.append(
            Question(
                kind="contradiction",
                subject_key=f"{row['subject']}|{row['key']}",
                question=f"{row['subject']} · {row['key']} has been recorded more than one way. Which holds?",
                detail="\n".join(f"— {v}" for v in values),
                options=values,
            )
        )
    return out


def _priority(conn: sqlite3.Connection, settings: Settings, today: date) -> list[Question]:
    """When two things could not both fit, which should have won.

    The planner breaks ties by due date and then by age, which is a rule about dates and
    not about what matters. Asked only when the tie actually cost something — two items in
    the same priority band where one was placed and one was not — so this stays a question
    about a real outcome rather than an inventory of the owner's values.
    """
    from backglass.plan import planner

    proposal = planner.propose(conn, settings, today, events=[])
    placed = [b for b in proposal.blocks if b["kind"] in ("work", "protected")]
    if not placed or not proposal.overflow:
        return []

    dropped = proposal.overflow[0]
    last = placed[-1]
    if str(last["title"]) == dropped.what:
        return []
    return [
        Question(
            kind="priority",
            subject_key=f"{today.isoformat()}|{dropped.commitment_id}",
            question="This did not fit today. Should it have come first?",
            detail=(
                f"Did not fit: {dropped.what} ({dropped.minutes}m)\n"
                f"Was planned instead: {last['title']}\n"
                "The planner ranks by due date and then by age, which is a rule about "
                "dates rather than about what matters to you."
            ),
            options=[
                f"Yes — {dropped.what[:40]} matters more",
                "No — the plan had it right",
            ],
        )
    ]


DETECTORS = ("conflict", "untitled", "duplicate_entity", "contradiction", "priority")


#: A commitment this far past due, with nothing in the ledger mentioning it since,
#: is a candidate for "quietly no longer true". Two weeks, because the board already
#: nags inside that window and the recheck pass owns conversations; this detector is
#: for the mail-shaped obligation that decays with no thread to re-read.
STALE_OVERDUE_DAYS = 14
STALE_SILENCE_DAYS = 14

#: New stale questions per refresh. The first run against a neglected board would
#: otherwise raise a hundred at once, and a wall of questions is how an owner stops
#: answering any (the overflow-list lesson, 2026-08-09). Oldest first; the rest queue
#: behind answers.
STALE_BATCH_LIMIT = 5

#: The stale question's options, matched EXACTLY by the answer hook — a reworded
#: option is an answer the hook cannot act on, so these are constants, not prose.
STALE_DONE = "Done — mark it resolved"
STALE_DROP = "No longer relevant — drop it"
STALE_KEEP = "Still on my plate — keep it open"


def _stale_commitments(
    conn: sqlite3.Connection, settings: Settings, today: date
) -> list[Question]:
    """Open commitments long past due that nothing has mentioned since.

    The chat recheck reads a conversation backwards to see whether a promise was
    answered; mail has no such thread, and silence there is even weaker evidence — so
    this NEVER closes anything. It asks, with the newest evidence cited, and the
    owner's click acts through the same actions the board uses (docs/11: proposals,
    not actions — the click is the owner's).

    Newest evidence is MAX over datetime() of the commitment's own source item and
    every commitment_evidence sighting — datetime(), not the raw column, because
    occurred_at keeps each sender's own offset and MAX over text picks the wrong
    message across the owner's two zones (lessons, 2026-08-02).
    """
    del settings
    overdue_floor = (today - timedelta(days=STALE_OVERDUE_DAYS)).isoformat()
    silence_floor = (today - timedelta(days=STALE_SILENCE_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT c.id, c.what, substr(c.due_at, 1, 10) AS due_day, c.direction,
               last.seen_day, last.source, last.title
        FROM commitment c
        JOIN (
          SELECT ranked.commitment_id,
                 substr(ranked.occurred_at, 1, 10) AS seen_day,
                 ranked.source AS source, ranked.title AS title
          FROM (
            SELECT c2.id AS commitment_id, si.occurred_at, si.source, si.title,
                   ROW_NUMBER() OVER (
                     PARTITION BY c2.id ORDER BY datetime(si.occurred_at) DESC
                   ) AS rn
            FROM commitment c2
            JOIN source_item si
              ON si.id = c2.source_item_id
              OR si.id IN (
                   SELECT ce.source_item_id FROM commitment_evidence ce
                   WHERE ce.commitment_id = c2.id AND ce.user_id = c2.user_id
                 )
            WHERE c2.user_id = :user_id AND c2.status = 'open'
          ) ranked
          WHERE ranked.rn = 1
        ) last ON last.commitment_id = c.id
        WHERE c.user_id = :user_id
          AND c.status = 'open'
          AND c.due_at IS NOT NULL
          AND substr(c.due_at, 1, 10) <= :overdue_floor
          AND last.seen_day <= :silence_floor
        ORDER BY substr(c.due_at, 1, 10) ASC, c.id ASC
        LIMIT :limit
        """,
        {
            "user_id": USER_ID,
            "overdue_floor": overdue_floor,
            "silence_floor": silence_floor,
            "limit": STALE_BATCH_LIMIT,
        },
    ).fetchall()

    out: list[Question] = []
    for row in rows:
        whose = "you owe" if row["direction"] == "i_owe" else "owed to you"
        out.append(
            Question(
                kind="stale",
                subject_key=str(row["id"]),
                question=(
                    f'"{row["what"]}" ({whose}) was due {row["due_day"]} and nothing '
                    "has mentioned it since. Still real?"
                ),
                detail=(
                    f"Newest evidence: {row['source']} · {row['seen_day']}"
                    + (f" · {row['title']}" if row["title"] else "")
                ),
                options=[STALE_DONE, STALE_DROP, STALE_KEEP],
            )
        )
    return out


#: Options for the protected-time question, matched exactly like the stale ones.
PROTECTED_GIVE = "Give it the protected time this once"
PROTECTED_HOLD = "The routine holds — it waits or overflows"


def _protected_conflicts(
    conn: sqlite3.Connection, settings: Settings, today: date
) -> list[Question]:
    """The goal's own example: school priority versus gym time, recognized, asked.

    When something due today (or overdue) did not fit the day, and a configured
    routine held at least that many minutes of it, the collision is between two things
    the owner has stated — the obligation and the standing routine — and no date rule
    can rank them. Never guessed: the planner keeps planning around routines until the
    owner answers. Ask-once identity is (commitment, routine name), not the day — a
    weekly gym slot colliding with the same problem set is one question, not one per
    week (the same-guest lesson: a signal constant across instances belongs in the
    identity, not asked repeatedly).
    """
    from backglass.plan import capacity as capacity_mod
    from backglass.plan import planner, timezones

    proposal = planner.propose(conn, settings, today)
    urgent = [c for c in proposal.overflow if c.priority <= 1]  # overdue or due today
    if not urgent:
        return []
    tz = timezones.active_tz(settings, today)
    routines = [
        e for e in capacity_mod.routine_events(settings, today, tz) if e.minutes > 0
    ]
    if not routines:
        return []

    out: list[Question] = []
    for cand in urgent[:3]:
        need = max(1, cand.minutes)
        blocker = next((r for r in routines if r.minutes >= need), None)
        if blocker is None:
            continue
        out.append(
            Question(
                kind="priority",
                subject_key=f"protected|{cand.commitment_id}|{blocker.title.lower()}",
                question=(
                    f'"{cand.what}" is due and did not fit today, while '
                    f"{blocker.title} holds {blocker.minutes}m. Which wins?"
                ),
                detail=(
                    f"Did not fit: {cand.what} ({need}m, "
                    f"{'overdue' if cand.priority == 0 else 'due today'})\n"
                    f"Routine: {blocker.title} "
                    f"{blocker.starts_at.strftime('%H:%M')}–"
                    f"{blocker.ends_at.strftime('%H:%M')}\n"
                    "The planner keeps planning around the routine until you answer."
                ),
                options=[PROTECTED_GIVE, PROTECTED_HOLD],
            )
        )
    return out


def detect(conn: sqlite3.Connection, settings: Settings, today: date) -> list[Question]:
    """Every detector, each failing on its own.

    Rule 5's shape: one detector that raises costs one kind of question, not the whole
    surface. A morning with no questions because something threw is indistinguishable
    from a morning with nothing to ask, and that is the failure this arrangement avoids.
    """
    found: list[Question] = []
    for detector in (
        lambda: _conflicts(conn, settings, today),
        lambda: _untitled(conn, settings, today),
        lambda: _duplicate_entities(conn),
        lambda: _contradictions(conn),
        lambda: _priority(conn, settings, today),
        lambda: _stale_commitments(conn, settings, today),
        lambda: _protected_conflicts(conn, settings, today),
    ):
        try:
            found.extend(detector())
        except Exception:  # noqa: BLE001 — one detector down is not the surface down
            continue
    return found


# ── the store ──────────────────────────────────────────────────────────────


def refresh(conn: sqlite3.Connection, settings: Settings, today: date) -> int:
    """Run the detectors and record anything not already asked. Returns the new count.

    `INSERT OR IGNORE` against the (kind, subject_key) unique index is the whole
    ask-once rule: a question already answered stays answered, and a question already
    open is not duplicated.
    """
    new = 0
    for question in detect(conn, settings, today):
        row = question.as_row()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO open_question"
            " (user_id, kind, subject_key, question, detail, options_json, asked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                USER_ID,
                row["kind"],
                row["subject_key"],
                row["question"],
                row["detail"],
                row["options_json"],
                now_iso(),
            ),
        )
        new += cursor.rowcount or 0
    return new


def open_questions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row, options=json.loads(str(row["options_json"])))
        for row in conn.execute(
            "SELECT * FROM open_question WHERE user_id = ? AND status = 'open'"
            " ORDER BY asked_at, id",
            (USER_ID,),
        )
    ]


def answer(
    conn: sqlite3.Connection,
    settings: Settings,
    question_id: int,
    *,
    option: str | None = None,
    text: str | None = None,
) -> None:
    """Record an answer, and record it durably where it settles something.

    An answer in the owner's own words is the interesting case, not the fallback, so
    `text` alone is a complete answer. The decision written alongside is what makes the
    answer outlive this table — `decisions.record` already carries supersession and
    provenance, and re-answering supersedes there exactly as it does here.
    """
    from backglass import decisions

    row = conn.execute(
        "SELECT * FROM open_question WHERE id = ? AND user_id = ?", (question_id, USER_ID)
    ).fetchone()
    if row is None:
        raise ValueError(f"no open question {question_id}")
    if not option and not text:
        raise ValueError("an answer needs an option or words of your own")

    conn.execute(
        "UPDATE open_question SET status = 'answered', answer_option = ?,"
        " answer_text = ?, answered_at = ? WHERE id = ?",
        (option, text, now_iso(), question_id),
    )
    decisions.record(
        conn,
        settings,
        title=str(row["question"]),
        choice=text or option or "",
        reasoning=f"answered in the questions surface · {row['kind']}",
    )
    _apply_stale_answer(conn, row, option)


def _apply_stale_answer(conn: sqlite3.Connection, row: Any, option: str | None) -> None:
    """A stale answer acts on the board — the click is the owner's (docs/11).

    Matched EXACTLY against the option constants: a free-text answer, or any option
    this function does not recognize, records the answer and touches nothing — never
    guess in the meantime. The actions re-read `status = 'open'` at write time, so a
    commitment closed since the question was asked is a no-op, not a crash.
    """
    if str(row["kind"]) != "stale" or option not in (STALE_DONE, STALE_DROP):
        return
    from backglass.web import actions

    try:
        commitment_id = int(str(row["subject_key"]))
    except ValueError:
        return
    note = "stale question: owner confirmed"
    try:
        if option == STALE_DONE:
            actions.resolve(conn, commitment_id, note=note)
        else:
            actions.drop(conn, commitment_id, note=note)
    except actions.ActionError:
        pass  # already closed by another surface; the recorded answer still stands


def dismiss(conn: sqlite3.Connection, question_id: int) -> None:
    """Not now. Distinct from answered: nothing is recorded as settled, and the question
    does not come back, because a question the owner has waved away twice is noise."""
    conn.execute(
        "UPDATE open_question SET status = 'dismissed', answered_at = ? WHERE id = ? AND user_id = ?",
        (now_iso(), question_id, USER_ID),
    )
