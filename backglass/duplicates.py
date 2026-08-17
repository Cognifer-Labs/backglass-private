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


def clusters(conn: sqlite3.Connection) -> list[Cluster]:
    """Suspect pairs collapsed into connected components, least certain last."""
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
    for pair in pairs:
        a, b = int(pair["a_id"]), int(pair["b_id"])
        union(a, b)
        weakest[(a, b)] = float(pair["score"])

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

    grouped: dict[int, Cluster] = {}
    for member in parent:
        if member not in rows:
            continue  # closed between the suspect pass and this read
        grouped.setdefault(find(member), Cluster()).members.append(rows[member])

    out: list[Cluster] = []
    for cluster in grouped.values():
        if len(cluster.members) < 2:
            continue
        ids = set(cluster.ids)
        inside = [s for (a, b), s in weakest.items() if a in ids and b in ids]
        cluster.weakest = min(inside) if inside else 0.0
        sources = {m["source_item_id"] for m in cluster.members}
        counterparties = {m["counterparty_entity_id"] for m in cluster.members}
        if len(sources) == 1 and len(counterparties) > 1:
            # One message, several counterparties. Three readings, and the label cannot
            # tell them apart, which is exactly why it never decides:
            #
            #   - three real promises (intro emails to three instructors),
            #   - one promise whose sender extraction resolved two ways
            #     ("complete 2026 Annual Education quiz", ids 88 and 211),
            #   - one promise to one organisation the entity table holds twice
            #     ("UW–Madison Financial Aid Office" and "University of
            #     Wisconsin–Madison"), which is an unmerged entity wearing this shape.
            #
            # The third is worth reading as a signal about `entity`, not about the
            # commitments: `backglass people merge` fixes the cause, and these clusters
            # then resolve themselves.
            cluster.kind = FAN_OUT
        cluster.members.sort(key=lambda m: int(m["id"]))
        out.append(cluster)

    # Identical text first, then tightest, then largest: what can be settled without
    # reading twice goes at the top, because the queue's failure was never that the
    # pairs were wrong.
    out.sort(key=lambda c: (not c.identical, -c.weakest, -len(c.members)))
    return out
