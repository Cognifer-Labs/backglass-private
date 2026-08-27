"""The owner's stated priorities, read from the knowledge base into the planner.

The planner has always ranked by dates — overdue, due today, due this week — which is a
rule about calendars, not about what matters to the owner. The goal (tasks/todo.md,
2026-08-18) names the missing half directly: "the app should understand user
preferences… school priority takes over gym time." The fact table is where the owner
states such things; this module is the typed lane between that statement and
`planner.order`.

One fact drives it: `preferences / planner.priority`, value shaped

    school: class, bio, homework > premed: mcat, anki > social: dinner, party

Lanes ordered by `>`, each optionally carrying its match keywords after `:` (a lane
with no keywords matches its own name). Matching is case-insensitive on the
commitment's `what`. The rank is a TIEBREAK inside the planner's date-derived priority
bands, never a replacement for them: "school beats social" cannot make an overdue
favour outrank an overdue problem set the other way around, because an overdue thing
is overdue whatever lane it lives in.

Malformed values degrade to no preference, loudly — the parse returns its complaints
and the planner notes them, because a preference silently ignored is worse than none:
the owner believes it is being applied.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

#: The fact key `lanes()` reads. Written like any fact:
#:   backglass memory set preferences planner.priority "school: class > social"
PRIORITY_KEY = "planner.priority"

#: Guardrails on the parse, not on the owner: a hundred lanes is a config accident.
MAX_LANES = 12
MAX_KEYWORDS = 12


@dataclass(frozen=True)
class Lane:
    name: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Preferences:
    lanes: tuple[Lane, ...] = ()
    #: What could not be parsed, in sentences the planner can surface.
    warnings: tuple[str, ...] = ()

    def rank(self, what: str) -> int:
        """0-based lane index of the first lane matching `what`; unmatched sorts after
        every matched lane (len), so stating a preference never *demotes* the things
        it does not mention relative to each other."""
        text = what.lower()
        for i, lane in enumerate(self.lanes):
            if any(_word_match(kw, text) for kw in lane.keywords):
                return i
        return len(self.lanes)


def _word_match(keyword: str, text: str) -> bool:
    """Word-boundary match — "art" must not fire inside "quarterly" (the banned-word
    lesson, 2026-07-30: substring checks on short words fail the innocent case)."""
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def load(conn: sqlite3.Connection) -> Preferences:
    """The owner's planner preferences, or the empty (inert) set."""
    row = conn.execute(
        "SELECT value FROM fact WHERE user_id = 1 AND subject = 'preferences'"
        " AND key = ? AND status = 'active'",
        (PRIORITY_KEY,),
    ).fetchone()
    if row is None:
        # The stated default, not an empty set. This function returned `Preferences()`
        # from August until 2026-08-24 because the fact it reads had never been written,
        # so every commitment ranked identically inside its date band and ties fell
        # through to age — a priority mechanism that had never once changed an outcome.
        # `plan/priority.py` holds the owner's ruled order; a fact still overrides it.
        from backglass.plan import priority

        return priority.lanes()
    return parse(str(row["value"]))


def parse(value: str) -> Preferences:
    lanes: list[Lane] = []
    warnings: list[str] = []
    for part in value.split(">"):
        part = part.strip()
        if not part:
            continue
        name, _, raw_keywords = part.partition(":")
        name = name.strip().lower()
        if not name or not re.fullmatch(r"[\w][\w\s-]*", name):
            warnings.append(f"unreadable lane {part!r} in preferences/{PRIORITY_KEY}")
            continue
        keywords = tuple(
            kw.strip().lower()
            for kw in raw_keywords.split(",")
            if kw.strip()
        )[:MAX_KEYWORDS] or (name,)
        lanes.append(Lane(name=name, keywords=keywords))
        if len(lanes) >= MAX_LANES:
            warnings.append(f"preferences/{PRIORITY_KEY} has more than {MAX_LANES} lanes;"
                            " the rest were ignored")
            break
    if not lanes and value.strip():
        warnings.append(f"preferences/{PRIORITY_KEY} could not be read: {value!r}")
    return Preferences(lanes=tuple(lanes), warnings=tuple(warnings))
