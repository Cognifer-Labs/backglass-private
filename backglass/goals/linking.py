"""Which long-term goal a day's work serves, decided once and recorded with its evidence.

The gap this closes, measured on 2026-08-23: the ledger held 8 goals, 4 roadmaps, 28 dated
steps and 61 targets, `goals.health` called five of the eight at risk — and **one open
commitment out of 387 carried a `goal_id`**. So `planner.PRIORITY_AT_RISK_GOAL`, a whole
priority tier, had exactly one row it could ever promote. Nothing was broken; the link had
just never been made at the scale the ledger grew to, because
`checkpoints.link_commitment` is the only writer of that column and both its callers are
somebody typing a command.

**A text matcher was tried first, and it is why this is a model pass.** Matching commitment
text against goal, target and step titles linked 78 of 387 — and 72 of those came from the
single token `submit`, unique to goal 8 only because that goal's title contains it. "Submit
MMR immunization records" is not an external scholarship. Strip the generic verbs and the
honest yield was six rows. The thing being decided is meaning, and a vocabulary match
cannot see it.

The shape is `extract/relevance.py`'s, because that module already solved this problem:

**Batched, and judged once.** One call carries the owner's goals and up to `BATCH`
unjudged obligations. The verdict is recorded in `claim_event`, so the next sync sends only
what has never been judged — two consecutive syncs over an unchanged ledger make one pass's
worth of calls and then none (rule 3). `claim_event` rather than a table of its own on
purpose: 0032's own header names this wiring as its intended use, and a new migration would
have cost a renumber across two branches for a column that is already expressible.

**Ids in, ids back.** The model is handed ledger ids and returns them, so no similarity
matcher is needed. Both 2026-08-12 guards apply — a returned id is intersected with the set
actually sent, and `status = 'open'` is re-read at write time.

**A quote, or no link.** A link must quote the obligation's own words, and the quote is
checked against the row. `commitment.goal_id` is a bare integer with no provenance of its
own, so without this a year-old link is a plan reprioritised for a reason nobody can
reconstruct — which is rule 1 applied to a column that predates it.

**Asymmetric, but the other way from `relevance.py`.** There a wrong verdict *drops* an
obligation, silently and irreversibly-feeling, so it is gated hard and the hesitant case
becomes a question. Here a wrong link promotes a row in tomorrow's plan: the owner sees it
the next morning, one `link_commitment(None)` undoes it, and nothing is deleted. So this
links on ordinary confidence and leaves the hesitant ones alone rather than asking — an
unlinked commitment is exactly the status quo, and thirty questions are already waiting.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from backglass.config import Settings
from backglass.extract.prompts import Prompt
from backglass.extract.schemas import GoalLinkResponse, json_schema
from backglass.ledger import USER_ID

PROMPT_NAME = "link-goals"

#: Obligations per call. Matches `relevance.BATCH` — same prompt shape, same reason: the
#: static half is the goals, so a bigger batch amortises it and a smaller one re-sends it.
BATCH = 25

#: Ceiling per run, so a first pass over a 387-row backlog spreads across syncs instead of
#: spending the whole cap in one. `relevance.PER_RUN`'s reasoning, unchanged.
PER_RUN = 100

#: How much of a target's title reaches the prompt. Targets are short by construction.
TITLE_LIMIT = 90

#: Causes written to `claim_event`. Both mean "judged", which is what makes the next run
#: skip the row; only the first also means "linked".
CAUSE_LINKED = "goal:linked"
CAUSE_NONE = "goal:none"

#: Sent, and the model said nothing about it. Not a verdict — recording one the model did
#: not give would be inventing it — but it has to be recorded, because otherwise an item
#: the model consistently skips is re-sent on every run for ever.
#:
#: Measured on the first live dry run: 16 verdicts came back for 25 obligations sent. Nine
#: rows were simply absent from the response, and `candidates` skips only *judged* rows, so
#: those nine would have been paid for on every sync until something else closed them.
CAUSE_UNANSWERED = "goal:unanswered"

#: How many times an obligation may be sent without an answer before the pass stops asking.
#: Two, because one silence is a bad batch and a second is the row.
MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class Link:
    commitment_id: int
    goal_id: int | None
    confidence: float
    quote: str | None
    reason: str | None


@dataclass
class Work:
    goals: list[dict[str, Any]]
    commitments: list[dict[str, Any]]

    @property
    def sent_ids(self) -> set[int]:
        return {int(c["id"]) for c in self.commitments}

    @property
    def goal_ids(self) -> set[int]:
        return {int(g["id"]) for g in self.goals}


@dataclass
class Report:
    judged: int = 0
    linked: int = 0
    discarded: int = 0
    #: Sent, and absent from the response. Not discarded — that is a verdict this pass
    #: refused; this is one it never received.
    unanswered: int = 0
    calls: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def goals_with_targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Active goals, each carrying the target titles that say what progress looks like.

    The titles are the point rather than decoration. "Get into a competitive med school"
    contains none of the words *shadowing*, *MCAT* or *clinical hours*; its targets do, and
    an obligation to call hospices about volunteering reaches that goal only through them.
    """
    rows = conn.execute(
        "SELECT id, title FROM goal WHERE user_id = ? AND status = 'active' ORDER BY id",
        (USER_ID,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        targets = [
            str(t["title"])[:TITLE_LIMIT]
            for t in conn.execute(
                "SELECT title FROM target WHERE goal_id = ? AND active = 1 ORDER BY id",
                (int(row["id"]),),
            )
        ]
        out.append({"id": int(row["id"]), "title": str(row["title"]), "targets": targets})
    return out


def candidates(conn: sqlite3.Connection, *, limit: int = PER_RUN) -> list[dict[str, Any]]:
    """Open obligations never judged for a goal, oldest first.

    The `NOT EXISTS` over `claim_event` is the judged-once rule and the whole of rule 3
    here. A row already carrying a `goal_id` is also skipped: the owner may have set it by
    hand, and a pass that re-judged what a person decided would be overwriting them.

    The attempt count is the other half, and it is not the same thing. A model that
    silently omits an obligation from its response has not judged it, so the first clause
    would send it again — and again, for ever. Two silences and the pass stops asking,
    which bounds the cost of a row the model will not answer about without ever putting a
    verdict in its mouth.
    """
    return [
        dict(row)
        for row in conn.execute(
            "SELECT c.id, c.what, c.due_at, s.title, s.author"
            "  FROM commitment c"
            "  JOIN source_item s ON s.id = c.source_item_id"
            " WHERE c.user_id = ? AND c.status = 'open' AND c.goal_id IS NULL"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM claim_event e"
            "      WHERE e.user_id = c.user_id AND e.subject_table = 'commitment'"
            "        AND e.subject_id = c.id AND e.cause IN (?, ?))"
            "   AND ("
            "     SELECT COUNT(*) FROM claim_event e2"
            "      WHERE e2.user_id = c.user_id AND e2.subject_table = 'commitment'"
            "        AND e2.subject_id = c.id AND e2.cause = ?) < ?"
            " ORDER BY c.id LIMIT ?",
            (USER_ID, CAUSE_LINKED, CAUSE_NONE, CAUSE_UNANSWERED, MAX_ATTEMPTS, limit),
        )
    ]


def render(work: Work, *, prompt: Prompt) -> tuple[str, str]:
    """The two prompt halves. Static first, so a caching backend pays for it once."""
    goals = "\n".join(
        f"[goal {g['id']}] {g['title']}"
        + ("".join(f"\n    target: {t}" for t in g["targets"]) if g["targets"] else "")
        for g in work.goals
    )
    commitments = "\n".join(
        f"[{c['id']}] {c['what']}"
        + (f" (due {str(c['due_at'])[:10]})" if c["due_at"] else "")
        + f"\n    from {c['author'] or '?'} · {c['title'] or ''}"
        for c in work.commitments
    )
    static, _ = prompt.split()
    return static, prompt.render_dynamic(goals=goals, commitments=commitments)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def parse(data: dict[str, Any], work: Work) -> tuple[list[Link], int]:
    """Validated links, and how many were thrown away.

    Discarded rather than repaired, in every case. A link this pass cannot fully stand
    behind is one it must not write, and a "best effort" fix is how an unjustified link
    gets recorded while looking careful.
    """
    parsed = GoalLinkResponse.model_validate(data)
    sources = {
        int(c["id"]): _normalize(f"{c['what']} {c['title'] or ''}")
        for c in work.commitments
    }
    kept: list[Link] = []
    discarded = 0
    for v in parsed.links:
        # 1. An id the pass never sent — the tables overlap in range (2026-08-12).
        if v.commitment_id not in work.sent_ids:
            discarded += 1
            continue
        if v.goal_id is None:
            # "No goal" is the ordinary answer and needs no evidence: it changes nothing,
            # and demanding a quote for it would push the model toward inventing links.
            kept.append(_link(v))
            continue
        # 2. A goal that was not among those sent.
        if v.goal_id not in work.goal_ids:
            discarded += 1
            continue
        # 3. A link with no quote behind it.
        if not (v.quote or "").strip():
            discarded += 1
            continue
        # 4. A quote that is not in the obligation it claims to have read — the guard
        #    against a fluent paraphrase standing in for having read the thing.
        if _normalize(v.quote) not in sources.get(v.commitment_id, ""):
            discarded += 1
            continue
        kept.append(_link(v))
    return kept, discarded


def _link(v: Any) -> Link:
    return Link(
        commitment_id=int(v.commitment_id),
        goal_id=int(v.goal_id) if v.goal_id is not None else None,
        confidence=float(v.confidence),
        quote=(v.quote or "").strip() or None,
        reason=(v.reason or "").strip() or None,
    )


def apply(
    conn: sqlite3.Connection, links: list[Link], *, dry_run: bool = False
) -> tuple[int, list[str]]:
    """Write the links. Returns how many goals were set, and a line per link for the log.

    Every judgement is recorded, including `null` — that is what makes the next run skip
    the row, and a pass that only recorded its positives would re-ask about the other 300
    every sync forever.
    """
    from backglass import claim_events
    from backglass.goals import checkpoints

    linked = 0
    notes: list[str] = []
    for link in links:
        # Re-read at write time. The row may have been resolved by another pass between
        # the call going out and the answer coming back (2026-08-12).
        row = conn.execute(
            "SELECT status, goal_id, what FROM commitment WHERE id = ? AND user_id = ?",
            (link.commitment_id, USER_ID),
        ).fetchone()
        if row is None or str(row["status"]) != "open" or row["goal_id"] is not None:
            continue
        if link.goal_id is not None:
            linked += 1
            notes.append(
                f"commitment {link.commitment_id} → goal {link.goal_id} "
                f'({str(row["what"])[:44]}) — "{link.quote}"'
            )
        if dry_run:
            continue
        if link.goal_id is not None:
            checkpoints.link_commitment(conn, link.commitment_id, link.goal_id)
        claim_events.record(
            conn,
            subject_table="commitment",
            subject_id=link.commitment_id,
            field="goal_id" if link.goal_id is not None else None,
            old_value=None,
            new_value=str(link.goal_id) if link.goal_id is not None else None,
            cause=CAUSE_LINKED if link.goal_id is not None else CAUSE_NONE,
        )
    return linked, notes


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    client: Any,
    cap: Any,
    *,
    dry_run: bool = False,
    limit: int = PER_RUN,
) -> Report:
    """One pass. Quiet when there is nothing unjudged, which is the steady state."""
    from backglass.extract import prompts

    report = Report()
    goals = goals_with_targets(conn)
    pending = candidates(conn, limit=limit)
    if not goals or not pending:
        return report

    prompt = prompts.load(PROMPT_NAME)
    schema = json_schema(GoalLinkResponse)

    for start in range(0, len(pending), BATCH):
        if cap is not None and getattr(cap, "reached", False):
            report.errors.append("spend cap reached; the rest stay unjudged")
            break
        work = Work(goals=goals, commitments=pending[start : start + BATCH])
        static, dynamic = render(work, prompt=prompt)
        try:
            result = client.complete(
                system=static,
                user=dynamic,
                schema=schema,
                model=settings.model_extract,
                budget_usd=settings.per_call_budget_usd,
            )
            report.calls += 1
            if cap is not None:
                cap.charge(result.cost_usd)
            links, discarded = parse(result.data, work)
        except Exception as exc:  # noqa: BLE001 — rule 5: one batch is not the pass
            report.errors.append(f"batch {start // BATCH}: {type(exc).__name__}: {exc}")
            continue
        report.discarded += discarded
        report.judged += len(links)
        # Sent and never mentioned. Counted before the writes so a batch that answered
        # about nothing still records that it was asked.
        answered = {link.commitment_id for link in links}
        silent = sorted(work.sent_ids - answered)
        report.unanswered += len(silent)
        if silent and not dry_run:
            from backglass import claim_events

            for cid in silent:
                claim_events.record(
                    conn, subject_table="commitment", subject_id=cid,
                    cause=CAUSE_UNANSWERED,
                )
        linked, notes = apply(conn, links, dry_run=dry_run)
        report.linked += linked
        report.notes.extend(notes)
    return report
