"""Duplicate suspects as clusters, because 264 pairs is not a queue anybody finishes.

`dedup.suspects` finds them and finds them well: on the owner's ledger it returns 264
open pairs, and the duplicates that cost real planner time are all in there. Its own
docstring names why that was not enough — *"a queue of 261 is one nobody reaches the end
of, the same failure the review queue had"*. The detection was never the problem.

Two things turn that queue into work someone can actually do.

**Clusters, not pairs.** Three restatements of one promise produce three pairs, and
answering them one at a time asks the same question three times and permits an
inconsistent answer. The connected components of the suspect graph are the real units:
seven rows about the hospice application are one decision.

**The fan-out fact, stated per cluster.** One message that promised an intro email to
three different instructors produces three rows with identical text that are three real
promises. `dedup._fan_out` detects the shape, and this refuses to resolve it for the
owner — measured on the live ledger, 4 of the 6 identical-text clusters are fan-out, and
collapsing them would have destroyed eight real commitments. But it is a fact, not a
verdict: `complete 2026 Annual Education quiz` is also same-source-different-person and
is plainly one quiz whose sender extraction resolved two ways. So the cluster is labelled
and the owner still answers.

Nothing here writes. Resolving goes through the paths that already exist and already
remember the answer — `web.actions.different` for "these are two promises",
`web.actions.resolve` for closing the losers — so an answer given here is never asked
again.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from backglass import dedup
from backglass.ledger import USER_ID

#: Same-source-different-counterparty, which is `dedup._fan_out`'s shape at cluster
#: scale. Named rather than inlined because it is the one label that changes what the
#: owner should do, and it must read the same way in the CLI and on the page.
FAN_OUT = "fan-out"
RESTATEMENT = "restatement"


def _normalized(what: str) -> str:
    """Word set, order and punctuation discarded — for the identical-text test only."""
    return " ".join(sorted(set(re.sub(r"[^a-z0-9 ]", " ", what.lower()).split())))


@dataclass
class PlanCluster:
    """Several rows describing one thing that is happening on one day.

    Engagements have no review surface at all — `dedup.suspects` and everything built on
    it covers commitments — and the duplication is worse there: six rows describe the
    McKenna kickoff dinner on 2026-08-09, and twenty-three describe one move-in. They
    reach the schedule page and the day plan, so the owner sees one dinner three times.

    Capacity is *not* wrong because of it — `_span_minutes` merges overlapping spans
    before measuring, so two copies of an 18:00–20:00 dinner cost 120 minutes and not
    240. This is a trust problem rather than an arithmetic one, which is why it reports
    and never writes.
    """

    day: str
    members: list[dict[str, Any]] = field(default_factory=list)
    weakest: float = 1.0

    @property
    def ids(self) -> list[int]:
        return [int(m["id"]) for m in self.members]

    @property
    def survivor(self) -> dict[str, Any]:
        """A display suggestion, never a write. `confirmed` outranks `proposed` — a plan
        the owner agreed to is the one worth keeping — then the row that says the most.
        """
        return sorted(
            self.members,
            key=lambda m: (
                0 if str(m["status"]) == "confirmed" else 1,
                -sum(1 for k in ("ends_at", "location") if m[k]),
                -len(str(m["what"])),
                int(m["id"]),
            ),
        )[0]


def plan_clusters(conn: sqlite3.Connection) -> tuple[list[PlanCluster], int]:
    """Same-day engagement duplicates, and how many undated look-alikes were left out.

    **Within one day only.** A weekly standing arrangement is the same words on many
    days and is emphatically not a duplicate — that is the engagement equivalent of the
    fan-out trap, and clustering across days would fuse every occurrence of a recurring
    dinner into one row.

    The day is `substr(starts_at, 1, 10)` and never `datetime()`. That column holds bare
    dates, naive local datetimes and offset-bearing strings side by side, because the
    resolver deliberately never converts zones — handing the mixture to SQLite's
    `datetime()` normalises the offset-bearing rows to UTC and marches a 19:00 Phoenix
    dinner into the next day, which is the failure recorded for 2026-08-01.

    Undated rows are counted and returned separately rather than clustered. Most of the
    twenty-three move-in rows have no `starts_at` at all, so there is no day to anchor
    them in and no honest way to tell one move-in from another — reporting the number
    keeps them visible instead of silently dropping them.
    """
    from backglass.extract import entities

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, what, starts_at, ends_at, location, status, confidence"
            "  FROM engagement"
            " WHERE user_id = ? AND status IN ('confirmed', 'proposed')"
            " ORDER BY id",
            (USER_ID,),
        )
    ]
    dated: dict[str, list[dict[str, Any]]] = {}
    undated = 0
    for row in rows:
        start = str(row["starts_at"] or "")
        if not start:
            undated += 1
            continue
        dated.setdefault(start[:10], []).append(row)

    out: list[PlanCluster] = []
    for day, group in sorted(dated.items()):
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        scores: dict[tuple[int, int], float] = {}
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                score = entities.similar(str(a["what"]), str(b["what"]))
                if score < dedup.SUSPECT_FLOOR:
                    continue
                scores[(int(a["id"]), int(b["id"]))] = score
                ra, rb = find(int(a["id"])), find(int(b["id"]))
                if ra != rb:
                    parent[rb] = ra

        grouped: dict[int, PlanCluster] = {}
        by_id = {int(r["id"]): r for r in group}
        for member in parent:
            grouped.setdefault(find(member), PlanCluster(day=day)).members.append(
                by_id[member]
            )
        for cluster in grouped.values():
            if len(cluster.members) < 2:
                continue
            ids = set(cluster.ids)
            inside = [s for (a, b), s in scores.items() if a in ids and b in ids]
            cluster.weakest = min(inside) if inside else 0.0
            cluster.members.sort(key=lambda m: int(m["id"]))
            out.append(cluster)

    out.sort(key=lambda c: (c.day, -len(c.members)))
    return out, undated


@dataclass
class Cluster:
    """One decision, and everything needed to make it without opening the ledger."""

    members: list[dict[str, Any]] = field(default_factory=list)
    #: The lowest pair score inside the cluster — how weakly it is held together.
    weakest: float = 1.0
    kind: str = RESTATEMENT
    #: The star's centre. Kept because `members` is sorted by id for reading and that
    #: throws away which row the others actually resembled. "Keep apart" needs it: the
    #: guarantee is one hop, so the flagged pairs are centre-to-each and nothing else,
    #: and recording every combination would assert a judgement the owner never made
    #: about two rows that were never compared.
    centre_id: int = 0

    @property
    def ids(self) -> list[int]:
        return [int(m["id"]) for m in self.members]

    @property
    def identical(self) -> bool:
        """Every member is the same sentence with the words in a different order."""
        return len({_normalized(str(m["what"])) for m in self.members}) == 1

    @property
    def survivor(self) -> dict[str, Any]:
        """The row worth keeping: the one that says the most.

        Longest text, ties broken by the lowest id. "Submit Hospice of the Valley
        volunteer application at volunteers.hov.org — gates the college orientation date"
        and "submit volunteer application online" are one promise, and the first is the
        one worth having in the morning.
        """
        return sorted(
            self.members, key=lambda m: (-len(str(m["what"])), int(m["id"]))
        )[0]

    @property
    def losers(self) -> list[dict[str, Any]]:
        keep = int(self.survivor["id"])
        return [m for m in self.members if int(m["id"]) != keep]

    def note(self) -> str:
        """What to record on each closed row, so the ledger says why it went."""
        return f"duplicate of commitment {self.survivor['id']}"

    @property
    def pairs(self) -> list[tuple[int, int]]:
        """The resemblances that were actually found: centre to each other member."""
        return [(self.centre_id, i) for i in self.ids if i != self.centre_id]

    @property
    def merged_into(self) -> int:
        """The id `actions.same_thing` will keep if the owner says these are one.

        The lowest, not `survivor`. Two different policies, both deliberate: `survivor`
        answers "which row says the most" for a person reading the card, and
        `same_thing` keeps the earliest because its citations reach furthest back. The
        card names this one, so what the owner reads is what the click does.
        """
        return min(self.ids)


def _stars(component: list[int], edges: dict[int, dict[int, float]]) -> list[list[int]]:
    """Split one connected component into stars: a centre, and its direct suspects only.

    Greedy by degree, ties by id so the same ledger always produces the same cards. The
    row the others actually resemble wins the centre, takes its neighbours with it, and
    whatever is left is split again — so a genuine group of seven survives intact while a
    chain of thirty-seven becomes the handful of real resemblances it was built from.

    A leftover whose only neighbours went with an earlier centre forms nothing, and its
    pair waits: `clusters` drops singletons, so that row is carded on the next read, after
    the card holding its neighbour is answered and the graph changes. Deferred, not lost —
    on the owner's ledger it is 7 rows of 194. Stated because a surface that quietly shows
    less than it found is the failure this file was written about.

    Not a clustering algorithm chosen for its properties; a guard chosen for what it
    refuses to produce. The guarantee is exactly one hop — every member is a suspect of
    the centre — so a cluster spans two rows that never resembled each other only when
    both resemble the same third row, which is a card a person can actually answer. What
    it rules out is the thirty-seven-member path, where the first and last members have
    nothing whatsoever to do with each other.
    """
    remaining = set(component)
    out: list[list[int]] = []
    while remaining:
        centre = max(
            remaining,
            key=lambda node: (len(edges.get(node, {}).keys() & remaining), -node),
        )
        star = [centre, *sorted(edges.get(centre, {}).keys() & remaining - {centre})]
        remaining -= set(star)
        out.append(star)
    return out


def clusters(conn: sqlite3.Connection) -> list[Cluster]:
    """Suspect pairs collapsed into stars, least certain last.

    Connected components were the first shape here and they chain. Similarity is not
    transitive: A resembles B and B resembles C says nothing about A and C, and a floor of
    0.6 over a personal ledger's whole open set makes that happen constantly. Measured on
    the owner's ledger on 2026-08-19: 194 of ~200 open rows fell into components, and one
    of them held **37 members** — the UT Dallas scholarship acceptance, a hospice
    volunteering application, an enrolment fee and an AP-credit transfer, presented as one
    decision. Nobody can answer that card, and a surface that asks it is worse than no
    surface, because it looks like the system believes it.

    So each emitted cluster is a *star*: one row, plus every row that is a suspect of that
    row directly. Members are chosen by degree, so the row the others actually resemble
    becomes the centre, and anything left over forms its own star. Seven rows that all
    look like the hospice application still arrive as one decision — that shape is
    unchanged, and its test says so. What cannot survive is a cluster held together by a
    path nobody would recognise as a resemblance.
    """
    pairs = dedup.suspects(conn)
    if not pairs:
        return []

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    weakest: dict[tuple[int, int], float] = {}
    #: The suspect graph itself, both directions. The components below are still what
    #: bounds the work; this is what says whether two members of one actually resemble
    #: each other or merely share a path.
    edges: dict[int, dict[int, float]] = {}
    for pair in pairs:
        a, b = int(pair["a_id"]), int(pair["b_id"])
        union(a, b)
        weakest[(a, b)] = float(pair["score"])
        edges.setdefault(a, {})[b] = float(pair["score"])
        edges.setdefault(b, {})[a] = float(pair["score"])

    rows = {
        int(r["id"]): dict(r)
        for r in conn.execute(
            "SELECT c.id, c.what, c.due_at, c.estimated_minutes, c.source_item_id,"
            "       c.counterparty_entity_id, e.canonical_name AS who"
            "  FROM commitment c"
            "  LEFT JOIN entity e ON e.id = c.counterparty_entity_id"
            " WHERE c.user_id = ? AND c.status = 'open'",
            (USER_ID,),
        )
    }

    grouped: dict[int, list[int]] = {}
    for member in parent:
        if member not in rows:
            continue  # closed between the suspect pass and this read
        grouped.setdefault(find(member), []).append(member)

    out: list[Cluster] = []
    for component in grouped.values():
        for star in _stars(component, edges):
            cluster = Cluster(members=[rows[i] for i in star], centre_id=star[0])
            if len(cluster.members) < 2:
                continue
            ids = set(cluster.ids)
            inside = [s for (a, b), s in weakest.items() if a in ids and b in ids]
            cluster.weakest = min(inside) if inside else 0.0
            sources = {m["source_item_id"] for m in cluster.members}
            counterparties = {m["counterparty_entity_id"] for m in cluster.members}
            if len(sources) == 1 and len(counterparties) > 1:
                # One message, several counterparties. Three readings, and the label
                # cannot tell them apart, which is exactly why it never decides:
                #
                #   - three real promises (intro emails to three instructors),
                #   - one promise whose sender extraction resolved two ways
                #     ("complete 2026 Annual Education quiz", ids 88 and 211),
                #   - one promise to one organisation the entity table holds twice
                #     ("UW–Madison Financial Aid Office" and "University of
                #     Wisconsin–Madison"), which is an unmerged entity wearing this shape.
                #
                # The third is worth reading as a signal about `entity`, not about the
                # commitments: `backglass people merge` fixes the cause, and these
                # clusters then resolve themselves.
                cluster.kind = FAN_OUT
            cluster.members.sort(key=lambda m: int(m["id"]))
            out.append(cluster)

    # Identical text first, then tightest, then largest: what can be settled without
    # reading twice goes at the top, because the queue's failure was never that the
    # pairs were wrong.
    out.sort(key=lambda c: (not c.identical, -c.weakest, -len(c.members)))
    return out


# ── the review surface, off the terminal ──────────────────────────────────────
# Everything above was reachable only by typing `backglass duplicates`. On the owner's
# ledger that is 73 clusters over 386 open commitments that nothing on the page has ever
# shown — the planner schedules whatever the ledger holds, so one scholarship acceptance
# written down five ways costs real hours of a real day, and the surface that could say
# so lived behind a command nobody runs unprompted.
#
# Cards, not collapses. `--apply` exists and stays a deliberate keystroke: a row that
# vanishes with nothing saying why is the failure four entries in tasks/lessons.md are
# about, and "identical text and not a fan-out" is a good heuristic rather than a fact.
# The click is the owner's (docs/11).

#: Options, matched exactly by the answer hook like every other question kind.
DUP_SAME = "One promise — merge them"
DUP_APART = "Different promises — keep them apart"

#: Cards per refresh. Five, like the stale batch, and for the same reason: a queue of 73
#: is a queue nobody finishes, and the whole point of the clustered shape was that each
#: card is answerable in one read.
DUP_BATCH_LIMIT = 5


def questions_for(conn: sqlite3.Connection, *, limit: int = DUP_BATCH_LIMIT) -> list[Any]:
    """The top clusters as questions, most certain first.

    Order is `clusters()`'s own: identical text first, then tightest, then largest — what
    can be settled without reading twice goes at the top. Fan-outs are not filtered out;
    they are the clusters that most need a person, and the card carries the sentence
    explaining what makes them ambiguous rather than a verdict.

    `subject_key` is the member id set, so ask-once holds while the cluster does and a
    cluster that gains or loses a member is a new question rather than a stale one
    wearing the old text.
    """
    from backglass.questions import Question

    out: list[Any] = []
    for cluster in clusters(conn)[:limit]:
        ids = cluster.ids
        keep = cluster.merged_into
        lines = [
            f"  #{m['id']} {str(m['what'])[:70]}" + (f"  · {m['who']}" if m["who"] else "")
            for m in cluster.members
        ]
        detail = "\n".join(lines) + f"\n\nMerging keeps #{keep}, the earliest — its"
        detail += " citations reach furthest back."
        if cluster.kind == FAN_OUT:
            detail += (
                "\n\nOne message, several counterparties: this is several real promises,"
                " or one organisation the entity table holds twice (`backglass people"
                " merge` fixes the second and these resolve themselves)."
            )
        out.append(
            Question(
                kind="duplicate",
                subject_key="-".join(str(i) for i in ids),
                question=(
                    f"{len(ids)} open commitments look like the same promise"
                    f" ({cluster.weakest:.2f} weakest match). One, or several?"
                ),
                detail=detail,
                options=[DUP_SAME, DUP_APART],
            )
        )
    return out
