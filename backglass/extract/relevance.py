"""Obligations the owner's own record has overtaken, retired against the fact that did it.

`logic.py` disposes of what the record contradicts *structurally* — text that reports
itself done, a question about a day that ended. This is the other half of the owner's
2026-08-18 instruction, and it needs a model: nothing in a UT Dallas scholarship deadline
is malformed. It was a real obligation, correctly extracted, and it stopped being owed the
moment the owner enrolled at ASU. The ledger knew that (`fact` row 5) and nothing read it.

Four properties, three of them borrowed from `recheck.py` because they are what makes an
automatic close survivable:

**Batched, and judged once.** One call carries the owner's facts and up to `BATCH` open
obligations; the verdict is stored per commitment in `logic_check`, so the next sync sends
only what has never been judged. Two hundred open rows cost one pass, not one per sync.

**Ids in, ids back.** The model is handed ledger ids and returns them, so no similarity
matcher is needed. Both 2026-08-12 guards apply: a returned id is intersected with the set
actually sent, and `status = 'open'` is re-read at write time.

**A citation, or no verdict.** `nonsense` must name the fact id it contradicts and quote
the obligation's own source text. The fact must exist and be active; the quote is checked
against the item. A verdict failing either is discarded, not repaired — "this looks
irrelevant" is precisely the reasoning that would delete the obligations the owner is
quietly failing, and it is the one failure here nobody would ever see.

**Asymmetric by consequence.** At or above `relevance_drop_confidence` the obligation is
dropped with the citation in its `resolution_note`; below it nothing is dropped and the
owner gets one question. A wrong keep costs a click. A wrong drop is silent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.extract.prompts import Prompt
from backglass.extract.schemas import RelevanceResponse, json_schema
from backglass.ledger import USER_ID

SCHEMA = json_schema(RelevanceResponse)

#: Obligations per call. Enough that one pass clears a neglected board, small enough that
#: a single bad call cannot take the whole ledger's judgement with it.
BATCH = 25

#: Obligations per run. The first pass over a two-hundred-row board is deliberately
#: several syncs' work: the spend cap is a hard stop (rule 7) and a checker that spends the
#: day's budget in one minute leaves extraction with nothing.
PER_RUN = 75

#: How much of the source text reaches the prompt. Enough to see who is asking and what
#: for; a scholarship letter's footer settles nothing.
BODY_LIMIT = 400


@dataclass
class Judgement:
    commitment_id: int
    verdict: str
    confidence: float
    cites_fact: int | None
    quote: str | None
    reason: str | None
    #: Fact ids this obligation's standing rests on, already intersected with the set the
    #: call was given. Empty means "rests on no recorded fact", which is an answer.
    depends_on: tuple[int, ...] = ()


@dataclass
class Work:
    """One call's worth: the owner's facts, the situation, and the obligations judged.

    `situation` is `backglass/situation.py`'s state doc minus its facts section, which
    this pass already carries under its own header. The facts alone say what is true; they
    do not say what recently stopped being true, what the week actually holds, or which
    obligations are already resting on which claim — and every one of those changes the
    answer to "has something the owner recorded made this moot?". Empty on a ledger with
    nothing to report, in which case the prompt renders exactly as it did at version 1.
    """

    facts: list[dict[str, Any]] = field(default_factory=list)
    commitments: list[dict[str, Any]] = field(default_factory=list)
    situation: str = ""

    @property
    def sent_ids(self) -> set[int]:
        return {int(c["id"]) for c in self.commitments}

    @property
    def fact_ids(self) -> set[int]:
        return {int(f["id"]) for f in self.facts}


@dataclass
class Report:
    judged: int = 0
    dropped: int = 0
    asked: int = 0
    kept: int = 0
    discarded: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    #: One line per obligation this pass would retire or ask about, with the fact behind
    #: it. Counts alone make `--dry-run` useless for the thing it exists for: reading a
    #: pass costs the same money as running it, so "1 dropped" that cannot be inspected
    #: buys nothing over just running it.
    verdicts: list[str] = field(default_factory=list)


def facts_for(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every active fact, with its id — the id is what a verdict has to cite.

    `facts.owner_context` renders the same rows for prompts that only need to read them.
    This pass needs them addressable, so it reads the table directly.
    """
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, subject, key, value FROM fact"
            " WHERE user_id = ? AND status = 'active' ORDER BY subject, key",
            (USER_ID,),
        )
    ]


def candidates(conn: sqlite3.Connection, *, limit: int = PER_RUN) -> list[dict[str, Any]]:
    """Open obligations never judged — or judged against a fact that has since moved.

    `LEFT JOIN logic_check` is the judged-once rule and the whole of rule 3 here: a run
    that judges everything leaves nothing for the next run to send, so two consecutive
    syncs over an unchanged ledger make exactly one pass's worth of calls and then none.

    **Judged once is not judged forever, since 2026-08-24.** A `keep` issued when the
    ledger said one thing was never revisited when it said another, which is the owner's
    complaint ("the checker has no way to notice the situation moved") restated in schema.
    A verdict is re-opened by exactly one condition: **every dependency it recorded has
    been broken.** `facts.remember` supersedes a fact, `claim_events.invalidate_fact`
    marks its dependents `superseded`, and a commitment whose recorded dependencies are
    all superseded is one whose verdict was reached from facts that no longer hold.

    Stated as row state rather than as "an event newer than the verdict", which is what
    this first was and could not work: `now_iso()` is second-resolution, so a fact
    superseded in the same second as the verdict was decided compares equal, and the
    fix in either direction is a loop or a miss. Row state has no such ambiguity and it
    clears itself — the re-judgment writes fresh `active` rows (a fact, or `none`), so the
    condition is false again the moment the obligation is judged, with no timestamp
    arithmetic anywhere.

    Narrow on purpose. Not "re-judge everything periodically", which is a bill; not "the
    world may have changed", which is unfalsifiable. One fact moved, and only what stood
    on it is re-asked.

    A `pending` verdict is never re-opened: it is already a question sitting in front of
    the owner, and asking the model again would both spend money on a decision that is
    waiting on a person and risk contradicting the question they are looking at.

    One backfill clause, and it is why the feature is not permanently empty: a commitment
    judged before v3 has a verdict and no dependency rows at all, so neither of the rules
    above would ever send it. On the owner's live ledger that was every open obligation —
    226 of them, judged in one pass on 2026-08-19 — and the index would have filled only
    from rows created after the prompt bump. It costs one pass over the backlog and then
    nothing: a v3 judgment always writes a row, so the clause is false for that commitment
    forever after.
    """
    return [
        dict(row)
        for row in conn.execute(
            "SELECT c.id, c.what, c.due_at, s.author, s.title, s.body_text, s.occurred_at"
            "  FROM commitment c"
            "  JOIN source_item s ON s.id = c.source_item_id"
            "  LEFT JOIN logic_check l ON l.commitment_id = c.id AND l.user_id = c.user_id"
            " WHERE c.user_id = ? AND c.status = 'open' AND c.direction = 'i_owe'"
            "   AND (l.id IS NULL"
            # Judged before v3 ever asked the question. Without this clause the
            # dependency index could only ever fill from commitments created after the
            # bump: every row already on the board carries a verdict, so it is never
            # re-queued, so it never records what it rests on, so nothing can ever
            # invalidate it — the feature would be live and permanently empty on the one
            # ledger it was built for. Self-clearing: a v3 judgment writes a row (a fact,
            # or `none`), and this clause is false for that commitment forever after.
            "        OR (l.status != 'pending' AND NOT EXISTS ("
            "             SELECT 1 FROM claim_dependency d"
            "              WHERE d.user_id = c.user_id AND d.subject_table = 'commitment'"
            "                AND d.subject_id = c.id))"
            "        OR (l.status != 'pending'"
            "        AND EXISTS (SELECT 1 FROM claim_dependency d"
            "                     WHERE d.user_id = c.user_id"
            "                       AND d.subject_table = 'commitment'"
            "                       AND d.subject_id = c.id AND d.status = 'superseded')"
            "        AND NOT EXISTS (SELECT 1 FROM claim_dependency d"
            "                         WHERE d.user_id = c.user_id"
            "                           AND d.subject_table = 'commitment'"
            "                           AND d.subject_id = c.id AND d.status = 'active')))"
            " ORDER BY c.id LIMIT ?",
            (USER_ID, limit),
        )
    ]


def render(work: Work, *, prompt: Prompt) -> tuple[str, str]:
    """The two prompt halves. Static first, so a caching backend pays for it once."""
    facts = "\n".join(
        f"[fact {f['id']}] {f['subject']} · {f['key']}: {f['value']}" for f in work.facts
    )
    commitments = "\n".join(
        f"[{c['id']}] {c['what']}"
        + (f" (due {str(c['due_at'])[:10]})" if c["due_at"] else "")
        + f"\n    from {c['author'] or '?'} · {str(c['occurred_at'])[:10]}"
        + f" · {c['title'] or ''}"
        + f"\n    source: {str(c['body_text'] or '')[:BODY_LIMIT]}"
        for c in work.commitments
    )
    static, _ = prompt.split()
    return static, prompt.render_dynamic(
        facts=facts,
        situation=work.situation or "(nothing else recorded)",
        commitments=commitments,
    )


def parse(data: dict[str, Any], work: Work) -> tuple[list[Judgement], int]:
    """Validated verdicts, and how many were thrown away.

    Discarded rather than corrected, in every case. A verdict this pass cannot fully
    trust is one it must not act on, and a "best effort" repair is how a wrong drop gets
    written while looking careful.
    """
    parsed = RelevanceResponse.model_validate(data)
    sources = {
        int(c["id"]): _normalize(f"{c['what']} {c['title'] or ''} {c['body_text'] or ''}")
        for c in work.commitments
    }
    kept: list[Judgement] = []
    discarded = 0
    for v in parsed.verdicts:
        # 1. An id the pass never sent — the tables overlap in range (2026-08-12).
        if v.commitment_id not in work.sent_ids:
            discarded += 1
            continue
        if v.verdict == "keep":
            kept.append(_judgement(v, work.fact_ids))
            continue
        # 2. A drop with no fact behind it. This pass exists to act on a recorded
        #    contradiction; without one it is just an opinion about an old row.
        if v.cites_fact is None or not (v.quote or "").strip():
            discarded += 1
            continue
        # 3. A fact id that is not one of the active facts sent.
        if v.cites_fact not in work.fact_ids:
            discarded += 1
            continue
        # 4. A quote that is not in the obligation it retires — the guard against a
        #    fluent paraphrase standing in for having read the thing.
        if _normalize(v.quote or "") not in sources.get(v.commitment_id, ""):
            discarded += 1
            continue
        kept.append(_judgement(v, work.fact_ids))
    return kept, discarded


def _judgement(v: Any, fact_ids: set[int] | None = None) -> Judgement:
    # Intersected with the facts actually sent, the same 2026-08-12 guard `cites_fact`
    # gets: the tables overlap in range, so an id the model invented or half-remembered
    # would otherwise become a dependency pointing at an unrelated row — and a wrong
    # dependency is what later decides an obligation is dead.
    #
    # Filtered rather than discarded, unlike a bad `cites_fact`. A citation is the
    # justification for closing something and must be right or absent; a dependency is a
    # note about what to re-check later, and dropping one bad id out of three leaves a
    # narrower index, not a wrong verdict.
    raw = tuple(int(i) for i in (getattr(v, "depends_on", None) or []))
    depends = tuple(dict.fromkeys(i for i in raw if fact_ids is None or i in fact_ids))
    return Judgement(
        commitment_id=int(v.commitment_id),
        verdict=str(v.verdict),
        confidence=float(v.confidence),
        cites_fact=v.cites_fact,
        quote=v.quote,
        reason=v.reason,
        depends_on=depends,
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
    """Write what survived: a row per judgement, a drop or a question for the confident."""
    from backglass.web import actions

    report = Report()
    facts_by_id = {int(f["id"]): f for f in work.facts}
    for j in judgements:
        row = conn.execute(
            "SELECT status, what FROM commitment WHERE id = ? AND user_id = ?",
            (j.commitment_id, USER_ID),
        ).fetchone()
        if row is None or str(row["status"]) != "open":
            continue  # closed by another surface since the call went out
        report.judged += 1

        if j.verdict == "keep":
            report.kept += 1
            if not dry_run:
                _record(conn, j, status="kept")
                _record_dependencies(conn, j)
            continue

        fact = facts_by_id.get(int(j.cites_fact or 0), {})
        citation = f"{fact.get('subject')} · {fact.get('key')}: {fact.get('value')}"
        confident = j.confidence >= settings.relevance_drop_confidence
        report.verdicts.append(
            f"{'drop' if confident else 'ask'} [{j.commitment_id}] {row['what']}"
            f"\n      against fact {j.cites_fact} — {citation}"
            f"\n      quoting: {(j.quote or '').strip()[:100]}"
        )
        if dry_run:
            report.dropped += confident
            report.asked += not confident
            continue

        _record(conn, j, status="applied" if confident else "pending")
        _record_dependencies(conn, j)
        if confident:
            actions.drop(
                conn,
                j.commitment_id,
                note=f"logic: contradicted by fact {j.cites_fact} — {citation}",
            )
            report.dropped += 1
        else:
            _ask(conn, j, str(row["what"]), citation)
            report.asked += 1
    return report


def _record_dependencies(conn: sqlite3.Connection, j: Judgement) -> None:
    """What this obligation rests on, into `claim_dependency` (0032).

    The point of asking on **every** verdict, keeps included: `logic_check` is judged once
    per commitment, so a keep issued when the facts said one thing was never revisited
    when they said another — "the checker has no way to notice the situation moved", which
    is the owner's own complaint restated in schema. These rows are the invalidation index
    that answers "which obligations did this fact hold up?", and nothing else in the
    ledger can.

    `none` is recorded, not omitted. An obligation judged to rest on no fact is a
    different thing from one nobody has judged, and `claim_events` states the rule this
    upholds: an empty result must read as *unknown*, never as *confirmed independent*.
    Writing the `none` row is what moves a commitment from the first to the second.
    """
    from backglass import claim_events

    if j.depends_on:
        for fact_id in j.depends_on:
            claim_events.depends_on_fact(
                conn,
                subject_table="commitment",
                subject_id=j.commitment_id,
                fact_id=fact_id,
                quote=(j.quote or "").strip(),
                reason=(j.reason or f"relevance verdict: {j.verdict}").strip(),
            )
        return
    claim_events.depends_on_none(
        conn,
        subject_table="commitment",
        subject_id=j.commitment_id,
        reason=(j.reason or "judged: rests on no recorded fact").strip(),
    )


def _record(conn: sqlite3.Connection, j: Judgement, *, status: str) -> None:
    """The verdict, and on a re-judgment the new one *replaces* the old.

    It used to be `DO NOTHING`, which was right while a commitment could only be judged
    once. Now that a broken dependency re-opens one (`candidates`), doing nothing would
    leave the stale verdict in place with its old `decided_at` — so the re-queue predicate
    would still be true on the next sync, and the same obligation would be re-judged, and
    re-paid for, every sync forever. A loop that only shows up as a bill.

    An UPDATE rather than a superseding row, and that is a compromise worth naming: house
    style is supersession, but `logic_check` is `UNIQUE (user_id, commitment_id)` and
    giving it history needs a migration, which re-arms the frozen-sidecar rebuild on a
    checkout the scheduler runs. The history goes to `claim_event` instead — which is what
    the change ledger is for, and it records the old verdict and the new one, so nothing
    is lost that the audit trail needs.
    """
    from backglass import claim_events

    previous = conn.execute(
        "SELECT verdict FROM logic_check WHERE user_id = ? AND commitment_id = ?",
        (USER_ID, j.commitment_id),
    ).fetchone()
    conn.execute(
        "INSERT INTO logic_check (user_id, commitment_id, verdict, confidence, fact_id,"
        " quote, reason, status, created_at, decided_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, commitment_id) DO UPDATE SET"
        "   verdict = excluded.verdict, confidence = excluded.confidence,"
        "   fact_id = excluded.fact_id, quote = excluded.quote, reason = excluded.reason,"
        "   status = excluded.status, decided_at = excluded.decided_at",
        (
            USER_ID, j.commitment_id, j.verdict, j.confidence, j.cites_fact, j.quote,
            j.reason, status, now_iso(), now_iso() if status != "pending" else None,
        ),
    )
    if previous is not None:
        claim_events.record(
            conn,
            subject_table="commitment",
            subject_id=j.commitment_id,
            cause="relevance_rejudged",
            field="logic_check.verdict",
            old_value=str(previous["verdict"]),
            new_value=j.verdict,
        )


#: The question's options, matched EXACTLY by the answer hook, like the stale ones.
RELEVANCE_DROP = "Right — drop it"
RELEVANCE_KEEP = "No, this is still mine — keep it"


def _ask(conn: sqlite3.Connection, j: Judgement, what: str, citation: str) -> None:
    """Below the threshold nothing is dropped; the owner gets one question instead."""
    conn.execute(
        "INSERT INTO open_question (user_id, kind, subject_key, question, detail,"
        " options_json, asked_at) VALUES (?, 'nonsense', ?, ?, ?, ?, ?)"
        " ON CONFLICT (user_id, kind, subject_key) DO NOTHING",
        (
            USER_ID,
            str(j.commitment_id),
            f'"{what}" looks overtaken by something you have already decided. Drop it?',
            f"Against: {citation}\n{j.reason or ''}".strip(),
            '["' + RELEVANCE_DROP + '", "' + RELEVANCE_KEEP + '"]',
            now_iso(),
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
    """One pass: everything never judged, in batches, each batch failing on its own."""
    report = Report()
    facts = facts_for(conn)
    pending = candidates(conn, limit=limit)
    if not facts or not pending:
        # With no recorded facts there is nothing to contradict, and this pass has no
        # business guessing from the obligations alone.
        return report

    # Rendered once for the whole pass, not once per batch: it is the same ledger for all
    # of them, and a block that differed between batches would defeat the caching the
    # static/dynamic split exists for. Best-effort — the judge worked without it at
    # version 1, and a doc that cannot render must not cost the pass its verdicts.
    situation = ""
    try:
        from backglass import situation as situation_mod
        from backglass.plan import timezones

        situation = situation_mod.render(
            conn, settings, timezones.local_now(settings).date(), include_facts=False
        )
    except Exception as exc:  # noqa: BLE001 — rule 5
        report.errors.append(f"situation: {type(exc).__name__}: {exc}")

    for start in range(0, len(pending), BATCH):
        work = Work(
            facts=facts,
            commitments=pending[start : start + BATCH],
            situation=situation,
        )
        try:
            static, user = render(work, prompt=prompt)
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
            report.dropped += written.dropped
            report.asked += written.asked
            report.kept += written.kept
            report.verdicts.extend(written.verdicts)
        except Exception as exc:  # noqa: BLE001 — rule 5, per batch
            report.errors.append(f"batch {start // BATCH}: {type(exc).__name__}: {exc}")
    return report
