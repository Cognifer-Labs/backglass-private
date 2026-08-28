"""Standing facts the record has moved past, proposed for revision against the item that did it.

2026-08-27, the owner: *"i no longer have trayfavors, the app should know this, why does it
not process things like these"*.

It knew, twice, and could act on neither. `fact` 59 said "Joined Tray Favors team December
2025", active, extracted on 2026-08-23 **from the email asking to leave Tray Favors** — the
poison gate worked exactly as designed, the sentence was quoted verbatim, the confidence
cleared 0.8, the lane was known. The fact was true when it was written. `fact` 86 said the
placement-change reply was "PREPARED … NOT YET SENT"; it was written at 10:52 and the reply
went out at 10:58, and three answers from the coordinator arrived that afternoon.

An obligation the record has overtaken has three mechanisms. `logic` throws out what is
structurally contradicted, `extract/relevance` retires what a recorded fact makes moot,
`extract/recheck` re-reads a chat commitment against what the conversation said next. A
**fact** the record has overtaken had none. `questions._contradictions` is the nearest
thing and it cannot fire here: it needs the same `(subject, key)` written twice with two
values, and nothing ever wrote a second one — the fact went stale in place.

Worse than inert. `facts.owner_context` rides into every model call, so the ledger has been
telling itself the owner is on Tray Favors with an unsent draft on every triage, extraction
and plan since the 24th.

**The asymmetry is inverted against `extract/relevance`, and that is the whole design.**
That pass may drop an obligation on its own above a threshold, because a wrong drop costs
one row the owner can see is missing and re-add. This one may never write anything active,
at any confidence: a wrong fact rewrite is not one bad row, it is a bad premise under every
call that follows, and it compounds silently. Every verdict here lands as an ordinary
`proposed` fact — the same status `facts.apply_extracted` writes for a candidate it does
not trust, on the same Memory page, accepted through the same `facts.accept`. There is one
door for "a fact becomes current" and the owner's click is it.

Four properties borrowed from `relevance.py`, because they are what makes an automatic
judgement survivable:

**Batched, and judged once.** One call carries a batch of active facts and the newest items
the ledger has read; the verdict is stored in `fact_check` keyed on `(fact_id,
through_item)`, so a fact is re-judged when — and only when — evidence has arrived since
the last time anyone looked at it.

**Ids in, ids back.** The model is handed ledger ids and returns them, so no similarity
matcher is needed. A returned id is intersected with the set actually sent.

**A citation, or no verdict.** `overtaken` must name the item id whose words overtook the
fact and quote them; the item must be one that was sent and the quote must appear in it. A
verdict failing either is discarded, not repaired.

**Silence is not evidence.** "Nobody has mentioned it lately" is `staleness`'s business and
it asks rather than acts. What this pass acts on is a positive statement in the record.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.extract.prompts import Prompt
from backglass.extract.schemas import RevisionResponse, json_schema
from backglass.ledger import USER_ID

SCHEMA = json_schema(RevisionResponse)

#: Facts per call. The whole knowledge base is 72 rows, so this is one or two calls for a
#: full pass — small enough that a single bad call cannot take every verdict with it.
BATCH = 20

#: Facts per run, for the same reason `relevance.PER_RUN` exists: the spend cap is a hard
#: stop (CLAUDE.md rule 7) and a checker that spends the day's budget leaves extraction
#: with nothing.
PER_RUN = 60

#: The newest kept items the judgement is formed against. A fact goes stale because
#: something recent said so, and handing the model a month of mail to reason over costs
#: more and decides less.
EVIDENCE_ITEMS = 40

#: How much of each item reaches the prompt. Enough to see who said what; a hospital
#: footer and four levels of quoted reply settle nothing.
BODY_LIMIT = 600


@dataclass
class Judgement:
    fact_id: int
    verdict: str
    confidence: float
    cites_item: int | None
    quote: str | None
    replacement: str | None
    reason: str | None


@dataclass
class Work:
    """One call's worth: standing facts, and the newest things the ledger has read."""

    facts: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sent_ids(self) -> set[int]:
        return {int(f["id"]) for f in self.facts}

    @property
    def item_ids(self) -> set[int]:
        return {int(i["id"]) for i in self.items}

    @property
    def through_item(self) -> int:
        """The newest item this judgement saw — the second half of the recurrence key."""
        return max(self.item_ids, default=0)


@dataclass
class Report:
    judged: int = 0
    proposed: int = 0
    current: int = 0
    discarded: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    #: One line per revision this pass would propose, with the words behind it. Counts
    #: alone make `--dry-run` useless for the thing it exists for.
    verdicts: list[str] = field(default_factory=list)


def evidence(conn: sqlite3.Connection, *, limit: int = EVIDENCE_ITEMS) -> list[dict[str, Any]]:
    """The newest kept items, which is what a fact can have been overtaken by.

    Kept only. A dropped item is one triage judged not to bear on the owner's life, and a
    pass that read them would be re-litigating that decision with a more expensive model.
    """
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, source, occurred_at, author, title, body_text FROM source_item"
            " WHERE user_id = ? AND triage_verdict = 'keep'"
            " ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (USER_ID, limit),
        )
    ]


def candidates(
    conn: sqlite3.Connection, *, through_item: int, limit: int = PER_RUN
) -> list[dict[str, Any]]:
    """Active facts never judged against evidence this new.

    The judged-once rule, and the reason the key is a pair. A watermark on the run would
    re-judge every fact on every sync and cost the same money for the same answers; a
    watermark on the fact alone would judge it once and never look again, which is the
    failure this whole module exists to fix. Keyed on `(fact_id, through_item)`, a fact is
    re-judged exactly when the ledger has read something since it was last considered.

    `NOT EXISTS` rather than `LEFT JOIN … IS NULL` because the pair is what is being
    tested, and the index on `(user_id, fact_id)` serves it directly.
    """
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, subject, key, value, note, source, created_at FROM fact f"
            " WHERE f.user_id = ? AND f.status = 'active'"
            "   AND NOT EXISTS (SELECT 1 FROM fact_check c"
            "                   WHERE c.user_id = f.user_id AND c.fact_id = f.id"
            "                     AND c.through_item >= ?)"
            " ORDER BY f.id LIMIT ?",
            (USER_ID, through_item, limit),
        )
    ]


def render(work: Work, *, prompt: Prompt, owner_emails: tuple[str, ...] = ()) -> tuple[str, str]:
    """The two prompt halves. Static first, so a caching backend pays for it once.

    Items the owner wrote are marked `YOU`, and that mark is load-bearing rather than
    decorative. The first live run over the real Tray Favors thread returned `current` for
    a fact that said a reply was "drafted in Mail but NOT YET SENT" while two of the items
    in front of it were that reply, sent. Nothing in the render said so: the author column
    held a bare address, and "this address is the owner, therefore the owner sent it,
    therefore it is no longer a draft" is three inferential steps to reach a fact the
    ledger already knows for certain. Rule 7 exists for exactly that shape, and it could
    not fire because the premise was never stated.
    """
    facts = "\n".join(
        f"[fact {f['id']}] {f['subject']} · {f['key']}: {f['value']}"
        + (f"\n    recorded {str(f['created_at'])[:10]} from: {f['note']}" if f["note"] else "")
        for f in work.facts
    )
    mine = {e.strip().lower() for e in owner_emails if e and e.strip()}
    items = "\n".join(
        f"[item {i['id']}] {str(i['occurred_at'])[:10]}"
        f" · {'YOU (the user wrote this)' if _is_owner(i['author'], mine) else (i['author'] or '?')}"
        f" · {i['title'] or ''}"
        f"\n    {' '.join(str(i['body_text'] or '').split())[:BODY_LIMIT]}"
        for i in work.items
    )
    static, _ = prompt.split()
    return static, prompt.render_dynamic(facts=facts, items=items)


def _is_owner(author: Any, mine: set[str]) -> bool:
    """Did the owner write this? Substring against the configured addresses, because an
    author field is `Name <addr>` as often as it is a bare address."""
    text = str(author or "").lower()
    return any(address in text for address in mine)


def parse(data: dict[str, Any], work: Work) -> tuple[list[Judgement], int]:
    """Validated verdicts, and how many were thrown away.

    Discarded rather than corrected, in every case. A verdict this pass cannot fully trust
    is one it must not raise, and a best-effort repair is how a wrong revision reaches the
    owner's Memory page looking careful.
    """
    parsed = RevisionResponse.model_validate(data)
    texts = {
        int(i["id"]): _normalize(f"{i['title'] or ''} {i['body_text'] or ''}")
        for i in work.items
    }
    kept: list[Judgement] = []
    discarded = 0
    for v in parsed.verdicts:
        # 1. A fact id the pass never sent — the tables overlap in range (2026-08-12).
        if v.fact_id not in work.sent_ids:
            discarded += 1
            continue
        if v.verdict == "current":
            kept.append(_judgement(v))
            continue
        # 2. A revision with nothing behind it. This pass exists to act on a positive
        #    statement in the record; without one it is an opinion about an old row.
        if v.cites_item is None or not (v.quote or "").strip():
            discarded += 1
            continue
        # 3. A replacement is required. "No longer true" is a deletion wearing a verdict's
        #    clothes, and the proposed row has to be able to stand alone in the ledger.
        if not (v.replacement or "").strip():
            discarded += 1
            continue
        # 4. An item id that is not one of the items sent.
        if v.cites_item not in work.item_ids:
            discarded += 1
            continue
        # 5. A quote that is not in the item it cites — the guard against a fluent
        #    paraphrase standing in for having read the thing that changed.
        if _normalize(v.quote or "") not in texts.get(v.cites_item, ""):
            discarded += 1
            continue
        kept.append(_judgement(v))
    return kept, discarded


def _judgement(v: Any) -> Judgement:
    return Judgement(
        fact_id=int(v.fact_id),
        verdict=str(v.verdict),
        confidence=float(v.confidence),
        cites_item=v.cites_item,
        quote=v.quote,
        replacement=v.replacement,
        reason=v.reason,
    )


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def apply(
    conn: sqlite3.Connection,
    settings: Settings,
    work: Work,
    judgements: list[Judgement],
    *,
    dry_run: bool = False,
) -> Report:
    """Write what survived: a `fact_check` row each, and a proposed fact for the overtaken.

    Nothing is superseded here and nothing is ever written `active`. The proposal goes in
    under the same `(subject, key)` as the fact it revises, so accepting it on the Memory
    page runs `facts.accept` → `remember` → supersession, which is the one path a fact has
    ever become current by.
    """
    report = Report()
    facts_by_id = {int(f["id"]): f for f in work.facts}
    items_by_id = {int(i["id"]): i for i in work.items}
    through = work.through_item
    for j in judgements:
        fact = facts_by_id.get(j.fact_id)
        if fact is None:
            continue
        live = conn.execute(
            "SELECT status FROM fact WHERE id = ? AND user_id = ?", (j.fact_id, USER_ID)
        ).fetchone()
        if live is None or str(live["status"]) != "active":
            continue  # superseded by another surface since the call went out
        report.judged += 1

        if j.verdict == "current":
            report.current += 1
            if not dry_run:
                _record(conn, j, through=through, status="current", proposed_fact=None)
            continue

        item = items_by_id.get(int(j.cites_item or 0), {})
        report.verdicts.append(
            f"revise [{j.fact_id}] {fact['subject']} · {fact['key']}"
            f"\n      was: {str(fact['value'])[:120]}"
            f"\n      now: {str(j.replacement or '')[:120]}"
            f"\n      per item {j.cites_item} ({str(item.get('occurred_at'))[:10]}"
            f" · {item.get('author') or '?'}): {(j.quote or '').strip()[:100]}"
        )
        if dry_run:
            report.proposed += 1
            continue

        proposed_id = _propose(conn, settings, j, fact, item)
        _record(
            conn, j, through=through,
            status="proposed" if proposed_id else "current",
            proposed_fact=proposed_id,
        )
        report.proposed += 1 if proposed_id else 0
    return report


def _propose(
    conn: sqlite3.Connection,
    settings: Settings,
    j: Judgement,
    fact: dict[str, Any],
    item: dict[str, Any],
) -> int | None:
    """The revision, as a `proposed` fact waiting on the owner's click.

    Written straight rather than through `facts.apply_extracted`, because that function's
    gate is about a candidate read out of one item's prose and this is a judgement about a
    row already in the ledger — but the *status* it writes is the same, the surface is the
    same, and `facts.accept` is the same. What differs is only how the candidate was
    arrived at, and the note says so, so a proposal on the Memory page can always be read
    back to the sentence that caused it.

    Returns None when an identical proposal is already waiting: asking twice is nagging,
    which is the same guard `apply_extracted` applies.
    """
    from backglass.plan import timezones

    value = str(j.replacement or "").strip()
    duplicate = conn.execute(
        "SELECT 1 FROM fact WHERE user_id = ? AND subject = ? AND key = ?"
        " AND value = ? AND status IN ('proposed', 'active')",
        (USER_ID, str(fact["subject"]), str(fact["key"]), value),
    ).fetchone()
    if duplicate is not None:
        return None
    note = (
        f"Revision: fact {j.fact_id} looks overtaken by item {j.cites_item}"
        f" ({str(item.get('occurred_at'))[:10]}, {item.get('author') or '?'})"
        f' — "{(j.quote or "").strip()[:200]}"'
    )
    conn.execute(
        "INSERT INTO fact (user_id, subject, key, value, note, source, source_item_id,"
        " confidence, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'extraction', ?, ?, 'proposed', ?)",
        (
            USER_ID, str(fact["subject"]), str(fact["key"]), value, note,
            j.cites_item, j.confidence, timezones.local_now_iso(settings),
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _record(
    conn: sqlite3.Connection,
    j: Judgement,
    *,
    through: int,
    status: str,
    proposed_fact: int | None,
) -> None:
    """The verdict, so the same evidence is never paid for twice.

    `DO NOTHING` on conflict rather than an update: the key is `(fact_id, through_item)`,
    so a conflict means this exact pair was already judged and the answer has not changed.
    """
    conn.execute(
        "INSERT INTO fact_check (user_id, fact_id, through_item, verdict, confidence,"
        " cites_item, quote, replacement, reason, proposed_fact, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (user_id, fact_id, through_item) DO NOTHING",
        (
            USER_ID, j.fact_id, through, j.verdict, j.confidence, j.cites_item,
            j.quote, j.replacement, j.reason, proposed_fact, status, now_iso(),
        ),
    )


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    client: Any,
    *,
    prompt: Prompt,
    dry_run: bool = False,
    limit: int = PER_RUN,
) -> Report:
    """One pass: every fact not yet judged against evidence this new, in batches."""
    report = Report()
    items = evidence(conn)
    if not items:
        # Nothing has been read, so nothing can have overtaken anything. This pass has no
        # business guessing from the facts alone.
        return report
    through = max(int(i["id"]) for i in items)
    pending = candidates(conn, through_item=through, limit=limit)
    if not pending:
        return report

    for start in range(0, len(pending), BATCH):
        work = Work(facts=pending[start : start + BATCH], items=items)
        try:
            static, user = render(
                work, prompt=prompt, owner_emails=tuple(settings.owner_emails or ())
            )
            result = client.complete(
                system=static,
                user=user,
                schema=SCHEMA,
                model=settings.model_extract,
                budget_usd=settings.per_call_budget_usd,
            )
            judgements, discarded = parse(result.data, work)
            report.cost_usd += float(getattr(result, "cost_usd", 0.0) or 0.0)
            report.discarded += discarded
            written = apply(conn, settings, work, judgements, dry_run=dry_run)
            report.judged += written.judged
            report.proposed += written.proposed
            report.current += written.current
            report.verdicts.extend(written.verdicts)
        except Exception as exc:  # noqa: BLE001 — rule 5, per batch
            report.errors.append(f"batch {start // BATCH}: {type(exc).__name__}: {exc}")
    return report
