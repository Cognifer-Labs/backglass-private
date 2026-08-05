"""The sync: ingest, then triage, then extract. docs/02 §Five stages.

Three properties this file exists to guarantee, all of them load-bearing:

  - **Idempotency** (CLAUDE.md rule 3). Two consecutive runs with no upstream changes
    produce zero writes. Every stage short-circuits on stored state: content_hash for
    ingest, triage_verdict for triage, extraction_version for extraction.
  - **Degrade, never block** (rule 5). A failing source is caught, recorded, surfaced,
    and the other sources continue. The process exits non-zero at the end.
  - **The spend cap is enforced, not monitored** (rule 7). Checked before every model
    call. On reaching it the run degrades to triage-only and sets `run.degraded`, rather
    than silently overspending.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backglass import chats as chats_mod
from backglass.config import Settings
from backglass.connectors import base, credentials
from backglass.connectors.base import Connector
from backglass.db import now_iso, query
from backglass.extract import commitments as tier2
from backglass.extract import engagements as engagement_tier2
from backglass.extract import noise as noise_mod
from backglass.extract import prompts, rules
from backglass.extract import triage as tier1
from backglass.extract.client import ModelAuthError, ModelClient, RateLimited
from backglass.extract.schemas import CommitmentExtraction
from backglass.ledger import USER_ID, Ledger

TRIAGE_PROMPT = "triage"
TRIAGE_BATCH_PROMPT = "triage-batch"
EXTRACT_PROMPT = "extract-commitments"


@dataclass
class SyncReport:
    fetched: int = 0
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    rule_dropped: int = 0
    model_triaged: int = 0
    triaged_out: int = 0
    batched: int = 0
    escalated: int = 0
    extracted: int = 0
    parked: int = 0
    commitments_inserted: int = 0
    commitments_deduped: int = 0
    commitments_superseded: int = 0
    engagements_inserted: int = 0
    engagements_advanced: int = 0
    #: Conversations seen for the first time and not yet decided about. Reported so the
    #: CLI can say a new chat is waiting rather than leaving it only on the dashboard.
    new_chats: int = 0
    new_chat_names: list[str] = field(default_factory=list)
    review_queue: int = 0
    writes: int = 0
    spend_cents: int = 0
    degraded: bool = False
    #: Why, when `degraded`. 'spend_cap' | 'rate_limit:triage' | 'rate_limit:extract'. A
    #: bare boolean made every surface assume the cap, so a rate-limited run would have
    #: asserted a month-long pause that was not happening and a reset date that meant
    #: nothing. Readers split on the colon: the stage refines the reason rather than
    #: sitting in a column beside it, so the two can never disagree, and anything a reader
    #: does not recognise — including a NULL row from before 0016 — falls through to the
    #: cap, which is the only pause those rows could have been.
    degrade_reason: str | None = None
    #: A model usage window closed mid-run. The items it touched are still PENDING, not
    #: parked, and no attempt was spent on them — see extract.client.RateLimited.
    rate_limited: bool = False
    #: Which stage the window closed in: 'triage' | 'extract'. The two leave completely
    #: different populations behind — triage-stranded items have no verdict at all, so no
    #: count over 'keep' items can see them — and only one of the two runs triage to
    #: completion, so a single sentence for both would assert the other stage's behaviour.
    rate_limited_stage: str | None = None
    #: The model backend rejected our credentials at least once this run. Distinct from
    #: `degraded` (the spend cap) and from a plain entry in `errors`: it means the items
    #: listed there were never read, so nothing about them was judged and there is
    #: nothing to review — the owner has to go re-authenticate the backend.
    #:
    #: Deliberately *not* a third value of `degrade_reason`, and not a setter of
    #: `rate_limited`. Those two describe a pause that ends on its own — the cap on the
    #: first of the month, a usage window in hours — and both leave the run honestly
    #: waiting. An outage ends when a human re-authenticates, and until then every later
    #: run fails the same way. Filed under the self-healing pauses it would inherit their
    #: sentence, and the owner would be told to wait for something that is not coming.
    model_auth_failed: bool = False
    failed_sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    date_notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """docs/02 §Failure policy: non-zero at the end so a scheduled run reports failure."""
        return 1 if self.failed_sources or self.errors else 0


class SpendCap:
    """docs/02 §Cost control. Hard, in code, checked before each call.

    Hard against *money*. A subscription backend reports what its calls would have cost
    on the API, and enforcing a dollar ceiling against that number stops work over a bill
    that will never arrive — which is exactly what happened on 2026-08-03, when nine
    consecutive syncs degraded to triage-only at 2006c of an unbilled 2000c. The spend is
    still recorded and still shown; `imputed` only decides whether it may stop anything.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        client: ModelClient | None = None,
    ):
        month_start = datetime.now(UTC).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        row = conn.execute(
            query("spend_this_month"),
            {"user_id": USER_ID, "month_start": month_start.isoformat()},
        ).fetchone()
        self.already_spent_cents = int(row["spend_cents"] if row else 0)
        self.cap_cents = settings.monthly_spend_cap_cents
        self.this_run_usd = 0.0
        # Absent a client, assume billed. A caller that does not say is a caller whose
        # backend we do not know, and the safe direction for a money guard is on.
        self.imputed = bool(getattr(client, "spend_is_imputed", False))

    @property
    def total_cents(self) -> int:
        return self.already_spent_cents + round(self.this_run_usd * 100)

    @property
    def reached(self) -> bool:
        return not self.imputed and self.total_cents >= self.cap_cents

    def charge(self, usd: float) -> None:
        self.this_run_usd += usd


def sync(
    conn: sqlite3.Connection,
    settings: Settings,
    connectors: list[Connector],
    client: ModelClient,
    *,
    dry_run: bool = False,
    extract: bool = True,
) -> SyncReport:
    """Run the pipeline. `extract=False` stops after triage — the batch-mode submit
    path (backglass/batch.py) reuses ingest, rules, and triage through here and hands
    extraction to the Batches API instead."""
    report = SyncReport()
    ledger = Ledger(conn, settings, dry_run=dry_run)
    cap = SpendCap(conn, settings, client)
    started_at = now_iso()

    _ingest(conn, ledger, connectors, report, dry_run=dry_run)
    _rule_pass(conn, ledger, settings, report)
    _triage_pass(conn, ledger, settings, client, cap, report)

    # Learned-noise auto-promotion, default off. Runs after triage so today's verdicts
    # count as evidence; promotions take effect on the *next* run's rule pass. Domain
    # candidates are never auto-promoted — CLI only.
    noise_writes = 0
    if settings.noise_auto_promote and not dry_run:
        addresses = [
            c
            for c in noise_mod.candidates(
                conn, settings, min_evidence=settings.noise_promote_after
            )
            if c.kind == "address"
        ]
        noise_writes = noise_mod.promote(conn, addresses, by="auto")

    # Not attempted once a usage window has closed: extraction calls the same backend that
    # just refused triage, so every item would come back RateLimited and the only product
    # of the pass would be a longer error list.
    if extract and not report.rate_limited:
        _extract_pass(conn, ledger, settings, client, cap, report)

    # Review-day tallies → checkpoints. Deterministic — the data arrives structured,
    # so this is code, not a model call, and it costs nothing on the cap. Skipped on
    # dry-run like every other write.
    review_writes = 0
    if not dry_run:
        from backglass.goals import reviews as reviews_mod

        review_writes = reviews_mod.sync_checkpoints(conn, settings)

    report.writes = ledger.writes + review_writes + noise_writes
    report.spend_cents = round(cap.this_run_usd * 100)
    # The cap wins the label when both hold: it is the one that persists past this run.
    if cap.reached:
        report.degraded, report.degrade_reason = True, "spend_cap"
    elif report.rate_limited:
        # Appended once, here, rather than per stage: triage and extraction can both trip
        # it and the owner does not need to be told twice. Rule 5 — surfaced, non-zero exit.
        #
        # The stage rides on the reason rather than in a column of its own. It is a
        # refinement of the same fact — which pause this was — and every reader already
        # threads exactly one string from `run.degrade_reason` to the sentence it picks. A
        # second column would need a second migration, a second argument through
        # record_run and three renderers, and could disagree with the first (a stage set
        # beside 'spend_cap'); a qualified reason cannot disagree with itself. Readers
        # split on the colon, so 'spend_cap', NULL, and any value they do not recognise
        # keep falling through to the behaviour they already had.
        stage = report.rate_limited_stage
        report.degraded = True
        report.degrade_reason = f"rate_limit:{stage}" if stage else "rate_limit"
        report.errors.append(
            f"model rate limit reached during {stage or 'the run'}; the remaining items "
            "are still pending (not parked) and the next scheduled sync retries them"
        )
    report.commitments_inserted = ledger.stats.commitments_inserted
    report.commitments_deduped = ledger.stats.commitments_deduped
    report.commitments_superseded = ledger.stats.commitments_superseded
    for conflict in ledger.stats.source_item_conflicts:
        report.errors.append(f"content changed for an immutable source_item: {conflict}")

    if not dry_run:
        _record_run(conn, report, started_at)
    return report


# ──────────────────────────────────────────────────────────────── stage 1


def _ingest(
    conn: sqlite3.Connection,
    ledger: Ledger,
    connectors: list[Connector],
    report: SyncReport,
    *,
    dry_run: bool,
) -> None:
    for connector in connectors:
        cursor = None
        record = credentials.load(conn, connector.name)
        if record is not None:
            cursor = record.cursor
        try:
            for item in connector.fetch(cursor):
                ledger.upsert_source_item(item)
                report.fetched += 1
            new_cursor = getattr(connector, "cursor", None)
            if not dry_run and new_cursor:
                credentials.save_cursor(conn, connector.name, str(new_cursor))
            if not dry_run:
                # docs/07 §Health: 'failed' stays until a *successful fetch* — this is
                # that fetch. Deliberately outside the cursor branch: a cursorless
                # connector (calendar:apple, files, notes) never reaches save_cursor,
                # and gating recovery on a cursor would leave exactly those sources
                # red forever.
                credentials.mark_ok(conn, connector.name)
        except Exception as exc:  # noqa: BLE001 - rule 5: degrade, never block
            # Redacted before it is persisted. This detail lands in credential.last_error
            # AND in run.errors_json — a table outside `credential`, which docs/08 says
            # tokens must never reach. Every connector had a redactor for its health()
            # path and none of them were wired to the path that actually writes.
            detail = base.safe_error(exc)
            report.failed_sources.append(connector.name)
            report.errors.append(f"{connector.name}: {detail}")
            if not dry_run:
                credentials.mark_failed(conn, connector.name, detail)
            continue

        report.excluded += getattr(connector, "excluded", 0)
        for rule, count in (getattr(connector, "excluded_by_rule", None) or {}).items():
            report.excluded_by_rule[rule] = report.excluded_by_rule.get(rule, 0) + count

        # What the connector saw, whether or not it was allowed to read it. Written here
        # rather than by the connector, on the same seam as the counters above: a
        # connector emits SourceItems and nothing else. A conversation nobody has named
        # lands undecided, which is what puts it on the prompt instead of dropping it.
        sightings = getattr(connector, "seen_chats", None)
        if sightings and not dry_run:
            seen = chats_mod.record(
                conn,
                connector.name,
                list(sightings.values()),
                # A connector that rescans a window reports a total, not an increment.
                cumulative=bool(getattr(connector, "sightings_are_cumulative", True)),
            )
            report.new_chats += seen.new
            report.new_chat_names.extend(seen.names)


# ──────────────────────────────────────────────────────────────── stage 2


def _rule_pass(
    conn: sqlite3.Connection, ledger: Ledger, settings: Settings, report: SyncReport
) -> None:
    """Tier 0. Free, deterministic, and it is what keeps the model bill small."""
    # Env-configured noise plus everything learned-and-promoted. classify's existing
    # address and subdomain matching covers both, so learned rows cost no new rule code.
    noise = frozenset(settings.noise_senders) | noise_mod.enabled_entries(conn)
    structured = frozenset(settings.structured_sources)
    pending = list(conn.execute(query("pending_triage"), {"user_id": USER_ID}))
    for item in pending:
        headers = _headers_of(item)
        verdict = rules.classify(
            headers=headers,
            author=str(item.get("author") or ""),
            raw_json=str(item.get("raw_json") or ""),
            body_text=item.get("body_text"),
            noise_senders=noise,
            source=str(item.get("source") or ""),
            structured_sources=structured,
        )
        if verdict.dropped:
            ledger.record_triage(int(item["id"]), "drop", verdict.reason)
            report.rule_dropped += 1
            report.triaged_out += 1
            continue

        # Template dedup: a sibling of a shape that has only ever been dropped, at
        # least `template_drop_after` times, dies here for free. One kept sibling,
        # ever, disqualifies the template — precision is not for sale, and same-run
        # cold starts (verdicts not yet written) simply escalate to the model.
        template = item.get("template_hash")
        if template:
            siblings = conn.execute(
                query("template_verdicts"),
                {"user_id": USER_ID, "template_hash": template, "id": int(item["id"])},
            ).fetchone()
            if (
                int(siblings["kept"]) == 0
                and int(siblings["dropped"]) >= settings.template_drop_after
            ):
                ledger.record_triage(int(item["id"]), "drop", f"template:{template[:8]}")
                report.rule_dropped += 1
                report.triaged_out += 1


class _LimitClaim:
    """One unit claiming a rate limit may be lying; a second one in the same pass is a wall.

    Stopping the wave is the right move when the window really is shut, and it costs
    almost nothing: the items are pending, and the next scheduled sync takes them. It
    costs everything when the window is not shut. A stopped wave consumes no attempt, so
    an item that fails deterministically in a way that reads like a limit stalls every
    item behind it on every later sync — forever — while the panel promises a retry. A
    silent permanent stall under a reassuring sentence is the failure this whole change
    exists to remove, so it must not rest on detection being perfect.

    Hence the cheapest bound that does not weaken the genuine case: a real window is shut
    for every call, so a second claimant always arrives. A lone one is held instead, and
    when the pass ends with nothing corroborating it, released down the ordinary failure
    path — parked or escalated, counted in the run's errors, bounded by the item's two
    attempts like any other failure. A per-run budget or a consecutive-degraded-run
    counter would bound it too, but both only cap how long the stall lasts; this refuses
    to start one, and needs no state outside the loop.

    Two *anywhere in the pass*, deliberately not two adjacent. Results are yielded in
    submission order while the calls run concurrently, so a window that closes mid-wave
    produces refusals in whatever order the threads were dispatched — a success can land
    between two genuine refusals. Adjacency would read that as a lone liar, park an item
    the window never let through, and let the run walk into the wall again on the next
    wave. Counting is indifferent to the ordering, and no weaker: a single poisoned item
    is still a single claim however the wave is scheduled.
    """

    def __init__(
        self,
        report: SyncReport,
        stage: str,
        park: Callable[[Any, Exception], None],
    ) -> None:
        self._report = report
        self._stage = stage
        self._park = park
        self._held: tuple[Any, Exception] | None = None

    def claimed(self, key: Any, exc: Exception) -> None:
        """A unit came back rate-limited. Confirm the wall, or hold the claim."""
        if self._report.rate_limited:
            return  # Already a wall. This one simply joins the items left pending.
        if self._held is None:
            self._held = (key, exc)
            return
        self._held = None  # Both claimants stay pending; neither spends an attempt.
        self._report.rate_limited = True
        self._report.rate_limited_stage = self._stage

    def settle(self) -> None:
        """The pass ended. A claim nothing corroborated was one unit's problem."""
        if self._held is not None:
            key, exc = self._held
            self._held = None
            self._park(key, exc)


def _triage_pass(
    conn: sqlite3.Connection,
    ledger: Ledger,
    settings: Settings,
    client: ModelClient,
    cap: SpendCap,
    report: SyncReport,
) -> None:
    prompt = prompts.load(TRIAGE_PROMPT)
    pending = list(conn.execute(query("pending_triage"), {"user_id": USER_ID}))
    if not pending:
        return

    # Batch first when the queue is big enough to pay for itself: instruction tokens
    # once per ~dozen items instead of once per item. Everything the batch cannot be
    # trusted on — uncertain, missing, disagreeing, or a failed batch — falls through
    # to the per-item loop below with the full 2000-char body, so nothing is ever
    # dropped on a 500-char excerpt the model hedged about. Below the threshold the
    # per-item path runs exactly as before.
    if len(pending) >= settings.triage_batch_min:
        pending = _batch_triage_pass(ledger, settings, client, cap, report, pending)
        if not pending or report.rate_limited:
            return

    def work(item: dict[str, Any]) -> tuple[int, tier1.TriageOutcome | Exception]:
        try:
            return int(item["id"]), tier1.triage(
                item,
                prompt=prompt,
                client=client,
                model=settings.model_triage,
                budget_usd=settings.per_call_budget_usd,
            )
        except Exception as exc:  # noqa: BLE001
            return int(item["id"]), exc

    def park(item_id: Any, exc: Exception) -> None:
        report.errors.append(f"triage {item_id}: {exc}")

    limit = _LimitClaim(report, "triage", park)
    for item_id, outcome in _in_parallel(
        work,
        pending,
        settings.max_concurrency,
        cap,
        stop_on_cap=False,
        stop_when=lambda: report.rate_limited,
    ):
        if isinstance(outcome, RateLimited):
            limit.claimed(item_id, outcome)
            continue
        if isinstance(outcome, Exception):
            # An auth failure is an ordinary park — it spends the item's attempts and
            # says so — and pointedly not a limit claim: `_LimitClaim` exists to stop the
            # wave and leave items pending for a window that reopens by itself, which is
            # the wrong promise entirely for a credential nobody has fixed yet.
            if isinstance(outcome, ModelAuthError):
                report.model_auth_failed = True
            park(item_id, outcome)
            continue
        # Charged and recorded even after the wall: these are siblings whose calls had
        # already returned when the limit landed. Their verdicts are real work, paid for
        # out of the same window, and throwing them away would both re-read them next sync
        # and leave run.spend_cents under-reporting on exactly the runs that burned quota.
        cap.charge(outcome.cost_usd)
        ledger.record_triage(item_id, outcome.verdict, outcome.reason)
        report.model_triaged += 1
        if outcome.verdict == "drop":
            report.triaged_out += 1
    limit.settle()  # Nothing corroborated a held claim.


def _batch_triage_pass(
    ledger: Ledger,
    settings: Settings,
    client: ModelClient,
    cap: SpendCap,
    report: SyncReport,
    pending: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run the batched tier-1 pass; return the items that still need per-item triage."""
    prompt = prompts.load(TRIAGE_BATCH_PROMPT)
    chunks = _pack(pending, settings)

    def work(
        chunk: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], tier1.BatchOutcome | Exception]:
        try:
            return chunk, tier1.triage_batch(
                chunk,
                prompt=prompt,
                client=client,
                model=settings.model_triage,
                budget_usd=settings.per_call_budget_usd,
            )
        except Exception as exc:  # noqa: BLE001
            return chunk, exc

    escalations: list[dict[str, Any]] = []

    def park(chunk: Any, exc: Exception) -> None:
        # A lone claimant is an ordinary failed batch, and a failed batch escalates.
        del exc
        escalations.extend(chunk)
        report.escalated += len(chunk)

    limit = _LimitClaim(report, "triage", park)
    for chunk, outcome in _in_parallel(
        work,
        chunks,
        settings.max_concurrency,
        cap,
        stop_on_cap=False,
        stop_when=lambda: report.rate_limited,
    ):
        if isinstance(outcome, RateLimited):
            # Confirmed claims are deliberately not escalated. Escalation exists because a
            # failed batch says nothing about its items and the per-item pass might still
            # read them; a shut window says the per-item pass cannot run either, so
            # escalating would only re-fail every item one at a time. Verdicts stay NULL.
            limit.claimed(chunk, outcome)
            continue
        if isinstance(outcome, Exception):
            # A failed batch proves nothing about its items: all of them re-read
            # per-item. Not an error — the fallback IS the failure handling.
            if isinstance(outcome, ModelAuthError):
                report.model_auth_failed = True
            escalations.extend(chunk)
            report.escalated += len(chunk)
            continue
        cap.charge(outcome.cost_usd)
        by_id = {int(item["id"]): item for item in chunk}
        for item_id, verdict in outcome.verdicts.items():
            ledger.record_triage(item_id, verdict.verdict, verdict.reason)
            report.model_triaged += 1
            report.batched += 1
            if verdict.verdict == "drop":
                report.triaged_out += 1
        for item_id in outcome.escalate:
            if item_id in by_id:
                escalations.append(by_id[item_id])
                report.escalated += 1
    limit.settle()  # Nothing corroborated a held claim.
    return escalations


# ──────────────────────────────────────────────────────────────── stage 3


def _extract_pass(
    conn: sqlite3.Connection,
    ledger: Ledger,
    settings: Settings,
    client: ModelClient,
    cap: SpendCap,
    report: SyncReport,
) -> None:
    prompt = prompts.load(EXTRACT_PROMPT)
    # "Unbatched": items riding a live `backglass batch` submission are excluded so
    # the scheduled sync never re-extracts them at full price while the half-price
    # batch is in flight. A batch older than 26h is presumed dead and its items
    # reappear here automatically (the query's degrade valve).
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(hours=26)).isoformat()
    pending = list(
        conn.execute(
            query("pending_extraction_unbatched"),
            {"user_id": USER_ID, "extraction_version": prompt.stamp, "cutoff": cutoff},
        )
    )
    if not pending:
        return

    if cap.reached:
        # docs/02: on_cap_reached → degrade to triage-only, log loudly, surface it.
        report.errors.append(
            f"spend cap reached ({cap.total_cents}c of {cap.cap_cents}c); "
            f"degraded to triage-only, {len(pending)} items left unextracted"
        )
        return

    # Fetched here, on the main thread, before anything is dispatched. `work` runs in a
    # thread pool and this connection is not shared across threads — the same reason the
    # results are written back on the main thread rather than inside the worker.
    contexts = {
        int(item["id"]): tier2.conversation_context(conn, int(item["id"]))
        for item in pending
    }

    def work(
        item: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[CommitmentExtraction, float] | Exception]:
        body = str(item.get("body_text") or "")
        if len(body) > settings.per_item_char_ceiling:
            return item, ValueError(
                f"body of {len(body)} chars exceeds the per-item ceiling "
                f"of {settings.per_item_char_ceiling}; parked"
            )
        try:
            return item, tier2.extract(
                item,
                prompt=prompt,
                client=client,
                model=settings.model_extract,
                budget_usd=settings.per_call_budget_usd,
                settings=settings,
                context=contexts.get(int(item["id"]), ""),
            )
        except Exception as exc:  # noqa: BLE001
            return item, exc

    def park(item: Any, exc: Exception) -> None:
        report.parked += 1
        report.errors.append(f"extract {int(item['id'])}: {exc}")

    limit = _LimitClaim(report, "extract", park)
    for item, outcome in _in_parallel(
        work,
        pending,
        settings.max_concurrency,
        cap,
        stop_on_cap=True,
        stop_when=lambda: report.rate_limited,
    ):
        item_id = int(item["id"])
        if isinstance(outcome, RateLimited):
            # The one exception to the park rule below, and the reason dropping the dollar
            # brake is safe: a shut usage window is not the item's fault, so it must not
            # consume the item's two attempts. Nothing is written, so extraction_version
            # stays unset and pending_extraction_unbatched still returns it next sync.
            limit.claimed(item, outcome)
            continue
        if isinstance(outcome, Exception):
            # extract-commitments.md §Failure handling: an item that fails is parked with
            # extraction_version unset, so a later prompt version retries it, and it is
            # surfaced rather than silently skipped. Shared with the park path a lone
            # unconfirmed limit claim takes, because it is the same handling — and an
            # auth failure takes it too, marked but never counted as a limit claim.
            if isinstance(outcome, ModelAuthError):
                report.model_auth_failed = True
            park(item, outcome)
            continue
        extraction, cost = outcome
        cap.charge(float(cost))
        # One transaction per item, for the same reason people/merge.py takes one: the
        # connection is autocommit, so apply()'s commitment inserts land immediately
        # while the extraction_version stamp that marks the item done lands after. A
        # crash in that gap (laptop sleep, OOM, a launchd restart mid-run) leaves the
        # rows written and the item still pending, so the next sync re-extracts it —
        # and the 0.85 dedup only catches a re-extraction the model phrases the same
        # way. Half-applied is worse than not applied: the owner gets a duplicate
        # commitment with no way to tell which one is real.
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = tier2.apply(
                extraction,
                source_item_id=item_id,
                occurred_at=str(item["occurred_at"]),
                ledger=ledger,
                settings=settings,
            )
            # Inside the same transaction as the commitments and the version stamp: one
            # response is one read of one message, and applying half of it is the
            # half-applied state the comment above rejects.
            plans = engagement_tier2.apply(
                extraction,
                source_item_id=item_id,
                occurred_at=str(item["occurred_at"]),
                ledger=ledger,
                settings=settings,
            )
            ledger.record_extraction_version(item_id, prompt.stamp)
        except Exception as exc:  # noqa: BLE001 - rule 5: degrade, never block
            # Rolled back, so the item is unstamped and untouched — the same parked state
            # a failed model call produces, and the next run retries it.
            #
            # This used to re-raise, and it was the one path in the pipeline that could
            # end a run: a single item whose apply() raised took every item behind it
            # with it. A 4,400-message backfill died on one malformed timestamp with
            # thousands of already-triaged items left unextracted, which is precisely the
            # "a failing source degrades, never blocks" rule applied one level too high —
            # the unit that fails here is a message, not a source.
            conn.execute("ROLLBACK")
            report.parked += 1
            report.errors.append(f"apply {item_id}: {base.safe_error(exc)}")
            continue
        conn.execute("COMMIT")
        report.extracted += 1
        report.review_queue += applied.review_queue + plans.review_queue
        report.engagements_inserted += plans.inserted
        report.engagements_advanced += plans.advanced
        report.date_notes.extend(applied.date_notes)
        report.date_notes.extend(plans.date_notes)
    limit.settle()  # Nothing corroborated a held claim.


# ──────────────────────────────────────────────────────────────── helpers


def _pack(
    pending: list[dict[str, Any]], settings: Settings
) -> list[list[dict[str, Any]]]:
    """Group items into batches by how much text they carry, not by how many there are.

    A fixed count is the wrong unit. The constraint on a batch is the context it has to
    fit into, and items vary by two orders of magnitude: a mail runs to the 500-character
    excerpt ceiling while an iMessage averages twenty-five. Packing both twelve at a time
    means a batch of texts uses a twentieth of the room it is paying instruction tokens
    for, so the owner's 3,687 messages cost 307 calls where a few dozen would do.

    `triage_batch_size` still sets the floor via the character budget below, so existing
    behaviour for mail-shaped items is unchanged: twelve items at the excerpt ceiling is
    exactly the budget. Short items simply pack denser. The per-batch item cap keeps a
    pathological run of one-word texts from building a batch the model loses track of —
    a verdict list it stops aligning to the ids is worse than an extra call.
    """
    budget = max(2, settings.triage_batch_size) * tier1.BATCH_BODY_LIMIT
    ceiling = max(2, settings.triage_batch_max_items)

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for item in pending:
        cost = min(len(str(item.get("body_text") or "")), tier1.BATCH_BODY_LIMIT)
        if current and (used + cost > budget or len(current) >= ceiling):
            chunks.append(current)
            current, used = [], 0
        current.append(item)
        used += cost
    if current:
        chunks.append(current)
    return chunks


def _in_parallel[T, R](
    work: Callable[[T], R],
    items: Sequence[T],
    max_workers: int,
    cap: SpendCap,
    *,
    stop_on_cap: bool,
    stop_when: Callable[[], bool] | None = None,
) -> Iterator[R]:
    """Run `work` over `items`, optionally stopping once the spend cap is reached.

    `stop_on_cap` is False for triage and True for extraction, because docs/02 §Cost
    control says the degraded state is *triage-only*, not stopped. Triage is the cheap
    tier and it is what keeps the ledger's view of the inbox complete; halting it as well
    would mean a month that hit the cap on the 8th silently stopped classifying mail for
    three weeks, and the backlog would look like an empty inbox rather than a paused one.

    Model calls take about five seconds each, so a first-run backfill would otherwise
    serialize into hours. Bounded because the Claude CLI backend spawns a subprocess per
    call and an unbounded fan-out would fork the machine to its knees.

    Results are yielded to the caller, which writes them on the main thread — sqlite3
    connections are not shared across threads here.

    The cap check has to interleave with the *results*, not sit in the submission loop.
    This is a generator: everything before the first `yield` runs on the caller's first
    `next()`, so a submission-loop check would evaluate `cap.reached` against spend from
    before this call and never against spend accrued inside it. The whole batch was
    submitted before the caller charged a cent — a 400-item backfill could run to twice
    the cap and only then report itself degraded, which is exactly the silent overspend
    docs/02 §Cost control and CLAUDE.md rule 7 exist to prevent.

    So work is submitted in waves of `max_workers`: each wave's results are yielded (and
    therefore charged) before the next wave is submitted. The cost of the fix is at most
    one wave of overshoot, which is the smallest overshoot possible without giving up
    concurrency altogether.

    `stop_when` is the same brake for a rate limit, and it is checked in the same place
    for a reason the caller cannot get right on its own. A caller that broke out of this
    loop would stop the wave it is *in*, and the rest of that wave was submitted before
    the limit landed: those calls ran, spent quota out of the very window being backed off
    from, and returned results that break discards — unwritten and uncharged, so the run
    under-reports its spend and the next sync pays for the same work again. Checked here,
    between waves, nothing new is started and everything already paid for is still yielded
    to the caller.
    """
    width = max(1, max_workers)
    remaining = list(items)
    with ThreadPoolExecutor(max_workers=width) as pool:
        lead = True
        while remaining:
            if stop_on_cap and cap.reached:
                return
            if stop_when is not None and stop_when():
                return
            # The first call of a pass goes alone. The Claude CLI backend authenticates
            # off a subscription token that it refreshes lazily, on first use, and
            # writes back to the login keychain — so a pass that opens by forking six
            # processes into an expired token has six of them attempting the same
            # refresh at once. Letting one go first means at most one process ever
            # performs the refresh and the rest start from the stored, fresh result.
            #
            # The same shape pays off against `stop_when` above, which is why the two
            # belong together rather than merely coexisting: a shut usage window is also
            # discovered by the first call that meets it, so opening with one means the
            # wall is found before five more calls are spent hitting it. Both brakes want
            # the cheapest possible probe of a condition that is identical for every unit.
            #
            # Traded: about one model call of wall clock per pass (~5s), and only when
            # there is more than one item. Not traded: steady-state concurrency, which
            # is what a lower `max_concurrency` would have cost instead.
            wave_width = 1 if lead else width
            lead = False
            wave, remaining = remaining[:wave_width], remaining[wave_width:]
            for future in [pool.submit(work, item) for item in wave]:
                yield future.result()


def _headers_of(item: dict[str, Any]) -> dict[str, str]:
    import json

    raw = item.get("raw_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return {}
    headers = parsed.get("headers") if isinstance(parsed, dict) else None
    if not isinstance(headers, dict):
        return {}
    return {str(k): str(v) for k, v in headers.items()}


def record_run(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    fetched: int = 0,
    triaged_out: int = 0,
    excluded: int = 0,
    extracted: int = 0,
    writes: int = 0,
    spend_cents: int = 0,
    degraded: bool = False,
    degrade_reason: str | None = None,
    errors: list[str] | None = None,
) -> None:
    """One run row. Shared with batch.py so `costs`, `status`, and spend_this_month
    see batch collections without knowing they exist."""
    import json

    conn.execute(
        "INSERT INTO run (user_id, started_at, finished_at, items_fetched, items_triaged_out, "
        " items_excluded, items_extracted, writes, spend_cents, degraded, degrade_reason, "
        " errors_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            USER_ID,
            started_at,
            now_iso(),
            fetched,
            triaged_out,
            excluded,
            extracted,
            writes,
            spend_cents,
            1 if degraded else 0,
            degrade_reason,
            json.dumps(errors) if errors else None,
        ),
    )


def _record_run(conn: sqlite3.Connection, report: SyncReport, started_at: str) -> None:
    record_run(
        conn,
        started_at=started_at,
        fetched=report.fetched,
        triaged_out=report.triaged_out,
        excluded=report.excluded,
        extracted=report.extracted,
        writes=report.writes,
        spend_cents=report.spend_cents,
        degraded=report.degraded,
        degrade_reason=report.degrade_reason,
        errors=report.errors or None,
    )
