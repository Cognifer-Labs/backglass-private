"""The conflict priority list: when two things want the same slot, what wins.

Owner's ruling, 2026-08-24: *"there should be a priority list in terms of conflicts."*

Before this the answer was distributed across four files and stated nowhere. Fixed events
won because `capacity` carved them out first; routines won because `day_events` placed
them before the planner ran; homework won over errands only if the owner had written a
`preferences/planner.priority` fact, and **they never had** — the lane mechanism shipped in
August with nothing in it, so every commitment ranked identically inside its date band and
ties fell through to age. The behaviour was real and defensible and no one could read it.

So the list is data now, in one place, in the owner's own order:

    1  fixed class / lab / exam         never moved
    2  exam or assignment due <48h      the thing that is about to be late
    3  gym, meals, sleep                protected
    4  homework due this week
    5  coding / OrgTruth
    6  clinical + premed admin
    7  errands, email, social

Three of those tiers were already true by construction and are written down here so the
list is complete rather than merely the part that needed code: tier 1 is `capacity`
carving fixed events out of the window, tier 3 is `day_events` placing routines before any
work, and tier 5 is the standing blocks being placed after the real work. This module's
own job is tiers 2, 4, 6 and 7 — the ordering *among commitments* — plus saying the whole
thing out loud on the schedule page.

**What this does not do, stated so it is not mistaken for an oversight.** Tier 2 sits above
tier 3, which reads as "a paper due tomorrow may take the gym hour". Nothing here moves a
meal or a workout automatically. A routine is the owner's own decision about their day and
`capacity.routine_events` already bounds how far one may drift; a planner that quietly
deleted dinner to fit an essay would be the surface nobody trusts again. What happens
instead is that the plan *says* it: when a tier-2 item cannot be placed, the notes name the
lower-tier blocks holding the time, in tier order, and the owner moves one. The list is a
statement of what matters, and the machine states it rather than enforcing it against them.

The default is overridable by the same fact the lane mechanism always read
(`preferences/planner.priority`), so the owner can restate the order without touching code:

    backglass memory set preferences planner.priority "school: class, exam > gym, lunch"
"""

from __future__ import annotations

from dataclasses import dataclass

from backglass.plan.preferences import Lane, Preferences

#: Hours-until-due that makes something tier 2 rather than tier 4. Two days, because that
#: is the window in which a thing stops being scheduled work and starts being the thing
#: that is about to be late.
IMMINENT_HOURS = 48


@dataclass(frozen=True)
class Tier:
    """One line of the list, and what it is about."""

    rank: int
    name: str
    #: What it matches in a commitment's text. Empty when the tier is not about
    #: commitments at all — tiers 1 and 3 are enforced by the day's shape, not by ranking.
    keywords: tuple[str, ...]
    #: How this tier wins, in one clause, for the surface that shows the list.
    enforced_by: str


#: The list, in the owner's order. Read top to bottom.
TIERS: tuple[Tier, ...] = (
    Tier(
        rank=1,
        name="fixed class, lab and exam",
        keywords=(),
        enforced_by="carved out of the window before anything is planned — never moved",
    ),
    Tier(
        rank=2,
        name=f"exam or assignment due within {IMMINENT_HOURS}h",
        keywords=("exam", "quiz", "midterm", "final", "test", "assignment", "homework"),
        enforced_by="ranked first among commitments; the plan names what is holding the "
                    "time when it does not fit",
    ),
    Tier(
        rank=3,
        name="gym, meals, sleep",
        keywords=(),
        enforced_by="placed as routines before any work is placed; never moved to fit work",
    ),
    Tier(
        rank=4,
        name="homework due this week",
        keywords=("chapter", "module", "lab report", "reading", "problem set", "essay",
                  "paper", "discussion", "canvas", "achieve", "learningcurve"),
        enforced_by="ranked above everything below it, and holds a reservation on the day",
    ),
    Tier(
        rank=5,
        name="coding / OrgTruth",
        keywords=("code", "coding", "orgtruth", "avorio", "backglass", "deploy", "ship",
                  "bug", "refactor"),
        enforced_by="a standing block, placed after the real work with what is left",
    ),
    Tier(
        rank=6,
        name="clinical and premed admin",
        keywords=("banner", "hospice", "volunteer", "shadow", "mcat", "anki", "clinical",
                  "premed", "amcas", "letter"),
        enforced_by="ranked above errands, below coursework",
    ),
    Tier(
        rank=7,
        name="errands, email, social",
        keywords=(),
        enforced_by="everything the list does not name sorts here",
    ),
)


def lanes() -> Preferences:
    """The list as the planner's existing lane ranking.

    `planner.order` has taken a `Preferences` since August and been handed an empty one
    ever since, because the fact it reads was never written. Projecting the default list
    into the same type means the ordering half of this needs no new code path in the
    planner and no second ranking that could disagree with the first — the list *is* the
    lanes.

    Tiers with no keywords are left out: they are not about commitment text, and a lane
    matching nothing would only shift the indices of the lanes below it.
    """
    return Preferences(
        lanes=tuple(
            Lane(name=tier.name, keywords=tier.keywords) for tier in TIERS if tier.keywords
        )
    )


def rank_of(what: str) -> int:
    """The tier number a commitment's text falls in, for display and for the notes.

    Returns the last tier's rank for anything unmatched — "errands, email, social" is
    where the list says unnamed things go, and that is a statement rather than a fallback.
    """
    text = " ".join(what.lower().split())
    for tier in TIERS:
        if tier.keywords and any(_matches(kw, text) for kw in tier.keywords):
            return tier.rank
    return TIERS[-1].rank


def _matches(keyword: str, text: str) -> bool:
    import re

    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None
