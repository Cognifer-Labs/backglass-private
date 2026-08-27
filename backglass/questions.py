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

import contextlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from backglass import staleness
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


#: The two answers to "you already decided this".
SETTLED_STANDS = "The decision stands — drop this"
SETTLED_NEW = "This is different — keep it"

#: The marker `answer()` writes into every decision it records. Lives here because this is
#: the module that writes it; `context.py` imports it rather than matching the wording a
#: second time.
ANSWERED_MARK = "answered in the questions surface"


def settles_an_obligation(decision: Any) -> bool:
    """Was this decision the owner stating a choice, rather than answering a question?

    Both readers of the decision table filter on this — the block every model call carries,
    and `_settled` below — and it is load-bearing for two separate reasons.

    **It breaks a loop.** Answering any question records a decision whose title is the
    question. So "Is 'Clean fishtank' still yours? — yes" becomes a standing decision that
    shares every word with the commitment it was about, and `_settled` would ask about it,
    and answering *that* would record another. A question generated from the answer to a
    question is the shape of an infinite surface.

    **It removes the noise.** "Are Nyasha and Mrs. Shepard the same person? — Same person"
    is a real decision and belongs in the audit trail, but it was applied when the entities
    merged and its words are generic enough to collide with anything: the UT Dallas merge
    matched two housing commitments through "university" and "housing", while the one
    decision that mattered matched one row.

    What is left is what the owner wrote down themselves — `backglass decide`, or the
    Decisions page. "BioBridge: I dropped it for the MLSBE summer program" is that, and it
    is the case this whole path exists for.
    """
    return ANSWERED_MARK not in str(getattr(decision, "reasoning", "") or "")

#: Words too common to carry a match on their own. A decision titled "Apply to the
#: scholarship" must not settle every commitment containing "apply" and "the".
_COMMON = frozenset(
    (
        "a", "an", "the", "to", "for", "of", "in", "on", "at", "and", "or", "is", "are",
        "be", "do", "this", "that", "it", "my", "your", "with", "about", "from",
        "submit", "complete", "send", "get", "ask", "make", "take", "pay",
    )
)

#: How many meaningful words a decision's title must share with a commitment before this
#: will ask about it.
_SETTLED_MIN_TOKENS = 2

#: …unless the one word they share is a name. The decision that motivated this detector is
#: titled "BioBridge" — one token, which the floor above rejects, and the natural shape for
#: a decision about a named thing.
#:
#: Length was the first attempt at "distinctive" and it is wrong: "application" is eleven
#: characters and settles nothing, so "Sallie Mae application" matched "Submit the housing
#: application". What separates the two is that BioBridge is capitalised in the title and
#: application is not. A proper noun names one thing; a long common noun names a category.
_SETTLED_NAME_CHARS = 4


def _shares_enough(shared: set[str], title: str) -> bool:
    """Two meaningful words in common, or one that the decision's title capitalises."""
    if len(shared) >= _SETTLED_MIN_TOKENS:
        return True
    names = {
        word.lower()
        for word in re.findall(r"[A-Za-z0-9]+", title)
        if word[:1].isupper() and len(word) >= _SETTLED_NAME_CHARS
    }
    return bool(shared & names)


def _which_first(created_at: str, decided_at: str) -> str:
    """Which came first, in words, for the question's detail.

    Days rather than instants: `decided_at` is local-with-offset and `created_at` is the
    ledger's UTC stamp, and comparing those two strings directly is the mixed-shape bug
    tasks/lessons.md 2026-08-01 is about. The leading ten characters are the local day
    under every shape the ledger holds, which is the resolution this sentence needs.

    Both directions matter and they mean different things. Older means the decision should
    have closed this and did not; newer means something re-opened what was settled.
    """
    if created_at[:10] < decided_at[:10]:
        return "before the decision — it should have been closed by it"
    return "after the decision — something re-opened it"


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _COMMON}


def _settled(conn: sqlite3.Connection) -> list[Question]:
    """A commitment that restates something the owner already decided against.

    The gap, 2026-08-23: `decisions.record` closes the commitment a decision settles, and
    nothing looks at the ones that arrive *afterwards*. The owner dropped BioBridge on 12
    August; a mail on the 20th about BioBridge orientation extracts as a live obligation,
    because the extractor is reading one message and the decision is in a table. Since the
    same day, `context._decided` puts standing decisions in front of every model call —
    this is the second line, for what still gets through.

    Asked, never disposed of. `logic.py`'s boundary is that nothing closes unless it can
    point at the row that contradicts it, and a shared-words match is not a contradiction:
    "I dropped BioBridge" and "return the BioBridge deposit" can both be true. So this
    surfaces the pair, quotes the decision, and lets the owner say which stands — and the
    answer is applied, so saying it once is enough.

    **Both directions, and the older one is the live failure.** The first draft of this
    only looked at commitments recorded *after* the decision, on the reasoning that a
    decision cannot settle something the owner had not yet been promised. That is
    backwards: a decision settles what already exists, and `decisions.record` closes only
    the one commitment explicitly handed to it. On this ledger the BioBridge decision was
    made on 12 August and "Withdraw from or confirm BioBridge Aug 5-15" — recorded on the
    2nd — is still open, which is precisely the row a reader of the decision would expect
    to be gone. So neither direction is filtered; the detail states which came first and
    lets the owner read it.

    Two bounds keep the queue from filling with near-misses. Never the commitment the
    decision itself closed — that is already handled, and re-asking it would be asking
    about its own answer. And the overlap has to clear `_shares_enough`: two meaningful
    words, or one the title capitalises.
    """
    from backglass import decisions

    standing = [d for d in decisions.active(conn) if d.choice and settles_an_obligation(d)]
    if not standing:
        return []

    rows = conn.execute(
        "SELECT id, what, created_at FROM commitment"
        " WHERE user_id = ? AND status = 'open' ORDER BY id",
        (USER_ID,),
    ).fetchall()
    if not rows:
        return []

    out: list[Question] = []
    for decision in standing:
        wanted = _tokens(decision.title)
        if not wanted:
            continue
        for row in rows:
            if decision.commitment_id == int(row["id"]):
                continue
            shared = wanted & _tokens(str(row["what"]))
            if not _shares_enough(shared, decision.title):
                continue
            out.append(
                Question(
                    kind="settled",
                    subject_key=f"{decision.decision_id}|{row['id']}",
                    question=(
                        f'You decided "{decision.title}" — is "{row["what"]}" still yours?'
                    ),
                    detail=(
                        f"decided {decision.decided_at[:10]}: {decision.choice}\n"
                        f"— this commitment was recorded {str(row['created_at'])[:10]}"
                        f" ({_which_first(str(row['created_at']), decision.decided_at)})"
                        f" and shares: {', '.join(sorted(shared))}"
                    ),
                    options=[SETTLED_STANDS, SETTLED_NEW],
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


#: The staleness rule itself lives in `backglass.staleness`, because the planner reads it
#: too: a commitment this surface is asking "still real?" about is one the planner must
#: stop scheduling in the meantime (increment 7). Re-exported here so the constants keep
#: the names every caller and test already uses.
STALE_OVERDUE_DAYS = staleness.STALE_OVERDUE_DAYS
STALE_SILENCE_DAYS = staleness.STALE_SILENCE_DAYS
STALE_BATCH_LIMIT = staleness.STALE_BATCH_LIMIT
STALE_DONE = staleness.STALE_DONE
STALE_DROP = staleness.STALE_DROP
STALE_KEEP = staleness.STALE_KEEP


def _stale_commitments(
    conn: sqlite3.Connection, settings: Settings, today: date
) -> list[Question]:
    """Open commitments long past due that nothing has mentioned since.

    The chat recheck reads a conversation backwards to see whether a promise was
    answered; mail has no such thread, and silence there is even weaker evidence — so
    this NEVER closes anything. It asks, with the newest evidence cited, and the
    owner's click acts through the same actions the board uses (docs/11: proposals,
    not actions — the click is the owner's).

    Batched: `STALE_BATCH_LIMIT` per refresh, oldest first. The planner's gate over the
    same predicate is not batched, so a board with a hundred lapsed rows stops proposing
    them today and is asked about them five at a time.
    """
    del settings
    rows = staleness.stale_rows(conn, today, limit=STALE_BATCH_LIMIT)

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


#: Distinct open commitments that must match a preset's signals before it volunteers
#: itself. One "volunteer application" is an errand; several signals across several
#: obligations is a life track running untracked.
ROADMAP_SIGNAL_MIN = 3

#: Matched exactly by the answer hook, like the stale options.
ROADMAP_START = "Start tracking it"
ROADMAP_NOT_THIS = "Not this path"


def _roadmap_candidates(
    conn: sqlite3.Connection, settings: Settings, today: date
) -> list[Question]:
    """Auto-detect a roadmap the owner is already living but not tracking.

    Reads the preset catalog's `signals:` words against open commitments and
    engagements (word-boundary; substring matching on short words is the 2026-07-30
    lesson). Three distinct matching records is the floor — below it this stays
    silent, because "only ask when necessary" is the rule that keeps the surface
    trusted. Ask-once identity is the preset id, so a dismissed path never returns;
    a preset with any existing roadmap row — active, done, or dropped — is never
    proposed, because all three mean the owner already decided.
    """
    import re as _re

    del today
    from backglass.roadmap import presets as presets_mod

    try:
        catalog = presets_mod.list_paths()
    except Exception:  # noqa: BLE001 — a broken preset file must not cost the surface
        return []
    tracked = {
        str(r["path_id"])
        for r in conn.execute("SELECT DISTINCT path_id FROM roadmap WHERE user_id = ?",
                              (USER_ID,))
    }
    rows = conn.execute(
        "SELECT what FROM commitment WHERE user_id = ? AND status = 'open'"
        " UNION ALL"
        " SELECT what FROM engagement WHERE user_id = ?"
        "  AND status IN ('proposed', 'confirmed')",
        (USER_ID, USER_ID),
    ).fetchall()
    texts = [str(r["what"]).lower() for r in rows]

    out: list[Question] = []
    seen_ids: set[str] = set()
    for preset in catalog:
        if not preset.signals or preset.id in tracked or preset.id in seen_ids:
            continue
        seen_ids.add(preset.id)  # medical.md and medical.public.md share an id
        matched = [
            t for t in texts
            if any(_re.search(rf"\b{_re.escape(sig)}\b", t) for sig in preset.signals)
        ]
        if len(matched) < ROADMAP_SIGNAL_MIN:
            continue
        shown = "\n".join(f"— {t[:80]}" for t in matched[:4])
        out.append(
            Question(
                kind="roadmap",
                subject_key=preset.id,
                question=(
                    f"{len(matched)} open items look like \"{preset.title}\" —"
                    " track it as a roadmap?"
                ),
                detail=(
                    f"{shown}\n"
                    f"Done means: {preset.definition_of_done}\n"
                    "Starting it creates the goal, its checkpoints and cadences;"
                    " saying no never asks again."
                ),
                options=[ROADMAP_START, ROADMAP_NOT_THIS],
            )
        )
    del settings
    return out


#: How many duplicate cards one refresh may raise. Same reasoning as `STALE_BATCH_LIMIT`:
#: the board already shows every suspect pair, so this queue exists to walk the owner
#: through the worst of them a few at a time, not to move the whole pile into /ask.
DUPLICATE_BATCH_LIMIT = 3

DUPLICATE_SAME = "Same promise — merge them"
DUPLICATE_DIFFERENT = "Different things — leave both"


def _duplicate_obligations(conn: sqlite3.Connection, settings: Settings) -> list[Question]:
    """One promise extracted several times, asked about rather than merged.

    The five-UT-Dallas-rows shape: commitments 64, 69, 73, 83 and 178 were five readings
    of one obligation, and the board's own note has said "dedup, a separate pass" since
    2026-08-18. Deliberately the only rule added in this pass that **cannot write**.
    Which row survives is a judgement — they differ in wording, in due date and in which
    message they came from — and `actions.same_thing` keeps the older row, folds the
    newer, and takes the earlier deadline. That is the right default and it is still a
    merge of two real rows; a checker that did it unasked would be guessing, and a wrong
    merge is not a click to undo.

    So this asks, and the owner's answer runs the same action the board's "Same thing"
    button runs. Its star clusters come from `duplicates.clusters`, which already refuses
    to chain (similarity is not transitive — a 37-member cluster is unanswerable), and
    only the centre and its strongest suspect are put in the question: two rows is a
    decision a person can make from one card.

    The floor is `dedup_threshold` — the score at which ingest merges two rows without
    asking anyone. Asking exactly there is the principled line: this queue is for the
    pairs the auto-joiner *would* have merged had they arrived together, which is what
    `dedup.suspects` already calls "the strongest kind of suspect". It was 0.75 for
    about an hour, until measuring real pairs showed "send the housing form to Barrett"
    and "send the housing deposit to Barrett" — two genuinely different obligations —
    scoring 0.83. A floor below the merge threshold asks the owner to adjudicate things
    the system itself would not have joined.
    """
    from backglass import duplicates

    out: list[Question] = []
    for cluster in duplicates.clusters(conn):
        if len(cluster.members) < 2 or cluster.weakest < settings.dedup_threshold:
            continue
        first, second = cluster.members[0], cluster.members[1]
        pair = sorted((int(first["id"]), int(second["id"])))
        out.append(
            Question(
                kind="duplicate_commitment",
                subject_key="|".join(str(p) for p in pair),
                question=(
                    f"Are “{_clip(str(first['what']))}” and “{_clip(str(second['what']))}” "
                    "the same promise?"
                ),
                detail=(
                    f"[{pair[0]}] {first['what']}\n[{pair[1]}] {second['what']}\n"
                    f"Merging keeps the older row and the earlier due date."
                ),
                options=[DUPLICATE_SAME, DUPLICATE_DIFFERENT],
            )
        )
        if len(out) >= DUPLICATE_BATCH_LIMIT:
            break
    return out


def _clip(text: str, *, at: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= at else text[: at - 1] + "…"


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
        lambda: _duplicate_obligations(conn, settings),
        lambda: _contradictions(conn),
        lambda: _settled(conn),
        lambda: _priority(conn, settings, today),
        lambda: _stale_commitments(conn, settings, today),
        lambda: _protected_conflicts(conn, settings, today),
        lambda: _roadmap_candidates(conn, settings, today),
    ):
        try:
            found.extend(detector())
        except Exception:  # noqa: BLE001 — one detector down is not the surface down
            continue
    return found


# ── the store ──────────────────────────────────────────────────────────────


def refresh(conn: sqlite3.Connection, settings: Settings, today: date) -> int:
    """Run the detectors and record anything not already asked. Returns the new count.

    The (kind, subject_key) unique index is the whole ask-once rule: a question already
    answered stays answered, and a question already open is not duplicated.

    One status is not final: `moot`. `logic.py` retires a question the world moved past —
    a collision that left the calendar, a day that ended — and if the same collision
    reappears in January it is a live question again, so re-detection revives that row in
    place. An owner's `dismissed` is never revived; waving something away twice is the
    owner saying it is noise, and the machine does not get to reopen that.
    """
    new = 0
    for question in detect(conn, settings, today):
        row = question.as_row()
        cursor = conn.execute(
            "INSERT INTO open_question"
            " (user_id, kind, subject_key, question, detail, options_json, asked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (user_id, kind, subject_key) DO UPDATE SET"
            "   status = 'open', question = excluded.question, detail = excluded.detail,"
            "   options_json = excluded.options_json, asked_at = excluded.asked_at,"
            "   answer_text = NULL, answered_at = NULL"
            " WHERE open_question.status = 'moot'",
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
    _apply_relevance_answer(conn, row, option)
    _apply_duplicate_answer(conn, row, option)
    _apply_roadmap_answer(conn, settings, row, option)
    _apply_settled_answer(conn, row, option)


def _apply_settled_answer(conn: sqlite3.Connection, row: Any, option: str | None) -> None:
    """"The decision stands" closes the commitment the decision had already settled.

    The point of asking is that answering ends it. Without this the owner confirms the
    obligation is dead and it stays open on the board, which teaches them that answering
    changes nothing — and a surface nobody believes acts is a surface nobody answers.

    Exact option match only, like the stale and relevance hooks: free text records the
    answer and touches nothing. The note names the decision id, so the drop is traceable
    back to the row that justified it rather than to "a question, once".
    """
    if str(row["kind"]) != "settled" or option != SETTLED_STANDS:
        return
    from backglass.web import actions

    decision_id, _, commitment_id = str(row["subject_key"]).partition("|")
    try:
        target = int(commitment_id)
    except ValueError:
        return
    with contextlib.suppress(actions.ActionError):
        actions.drop(conn, target, note=f"settled by decision {decision_id}: owner confirmed")


def _apply_roadmap_answer(
    conn: sqlite3.Connection, settings: Settings, row: Any, option: str | None
) -> None:
    """"Start tracking it" instantiates the preset — the owner's click, acted on.

    Exact option match only; free text records and creates nothing. A preset that
    vanished from disk since the question was asked records the answer and does
    nothing rather than raising — the decision row still says what the owner chose.
    """
    if str(row["kind"]) != "roadmap" or option != ROADMAP_START:
        return
    from backglass.plan import timezones
    from backglass.roadmap import instantiate as instantiate_mod
    from backglass.roadmap import presets as presets_mod

    try:
        preset = presets_mod.load(str(row["subject_key"]))
    except Exception:  # noqa: BLE001 — the answer stands; the preset is gone
        return
    if conn.execute(
        "SELECT 1 FROM roadmap WHERE user_id = ? AND path_id = ?",
        (USER_ID, preset.id),
    ).fetchone():
        return  # already tracked through another door; starting twice is a dup
    instantiate_mod.instantiate(
        conn, settings, preset, timezones.local_now(settings).date()
    )


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


def _apply_relevance_answer(conn: sqlite3.Connection, row: Any, option: str | None) -> None:
    """The half of the relevance judge that was not confident enough to act alone.

    The verdict is already recorded in `logic_check` as `pending`; the owner's click is
    what settles it, and settling it is also what stops the pass judging the same
    obligation again. "No, this is still mine" is therefore a durable answer, not a
    snooze — the row it protects is one the judge already suspected, and asking again next
    week would be asking a question the owner answered.
    """
    from backglass.extract.relevance import RELEVANCE_DROP, RELEVANCE_KEEP

    if str(row["kind"]) != "nonsense" or option not in (RELEVANCE_DROP, RELEVANCE_KEEP):
        return
    from backglass.web import actions

    try:
        commitment_id = int(str(row["subject_key"]))
    except ValueError:
        return
    conn.execute(
        "UPDATE logic_check SET status = ?, decided_at = ?"
        " WHERE commitment_id = ? AND user_id = ? AND status = 'pending'",
        (
            "applied" if option == RELEVANCE_DROP else "kept",
            now_iso(),
            commitment_id,
            USER_ID,
        ),
    )
    if option != RELEVANCE_DROP:
        return
    # Already closed by another surface is the ordinary case, not an error: the recorded
    # answer still stands either way.
    with contextlib.suppress(actions.ActionError):
        actions.drop(conn, commitment_id, note="logic: owner confirmed it is overtaken")


def _apply_duplicate_answer(conn: sqlite3.Connection, row: Any, option: str | None) -> None:
    """"Same promise" runs the merge the board's own button runs, and nothing else does.

    `Different` records the answer and writes nothing on purpose: `duplicates` re-derives
    its suspects from wording every time, so a pair the owner has separated would come
    back tomorrow — but the ask-once index on (kind, subject_key) is what stops it being
    *asked* again, which is the part that costs the owner anything.
    """
    if str(row["kind"]) != "duplicate_commitment" or option != DUPLICATE_SAME:
        return
    from backglass.web import actions

    try:
        a_id, b_id = (int(part) for part in str(row["subject_key"]).split("|"))
    except ValueError:
        return
    with contextlib.suppress(actions.ActionError):
        actions.same_thing(conn, a_id, b_id)


def dismiss(conn: sqlite3.Connection, question_id: int) -> None:
    """Not now. Distinct from answered: nothing is recorded as settled, and the question
    does not come back, because a question the owner has waved away twice is noise.

    One kind cannot simply be waved away, though, because something else is waiting on
    it. A `nonsense` question exists because the relevance judge suspected a row and was
    not confident enough to drop it; its `logic_check` row sits `pending` until an answer
    settles it, and nothing re-asks (the judge reads judged-once). Dismissing without
    settling would leave the obligation open, unjudgeable and unasked-about forever — so
    waving this one away is read as what it plainly means: leave my row alone.
    """
    row = conn.execute(
        "SELECT kind, subject_key FROM open_question WHERE id = ? AND user_id = ?",
        (question_id, USER_ID),
    ).fetchone()
    conn.execute(
        "UPDATE open_question SET status = 'dismissed', answered_at = ?"
        " WHERE id = ? AND user_id = ?",
        (now_iso(), question_id, USER_ID),
    )
    if row is None or str(row["kind"]) != "nonsense":
        return
    with contextlib.suppress(ValueError):
        conn.execute(
            "UPDATE logic_check SET status = 'kept', decided_at = ?"
            " WHERE commitment_id = ? AND user_id = ? AND status = 'pending'",
            (now_iso(), int(str(row["subject_key"])), USER_ID),
        )
