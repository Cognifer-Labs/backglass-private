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

from backglass.config import Settings
from backglass.connectors import credentials
from backglass.connectors.base import Connector
from backglass.db import now_iso, query
from backglass.extract import commitments as tier2
from backglass.extract import prompts, rules
from backglass.extract import triage as tier1
from backglass.extract.client import ModelClient
from backglass.extract.schemas import CommitmentExtraction
from backglass.ledger import USER_ID, Ledger

TRIAGE_PROMPT = "triage"
EXTRACT_PROMPT = "extract-commitments"


@dataclass
class SyncReport:
    fetched: int = 0
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    rule_dropped: int = 0
    model_triaged: int = 0
    triaged_out: int = 0
    extracted: int = 0
    parked: int = 0
    commitments_inserted: int = 0
    commitments_deduped: int = 0
    commitments_superseded: int = 0
    review_queue: int = 0
    writes: int = 0
    spend_cents: int = 0
    degraded: bool = False
    failed_sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    date_notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """docs/02 §Failure policy: non-zero at the end so a scheduled run reports failure."""
        return 1 if self.failed_sources or self.errors else 0


class SpendCap:
    """docs/02 §Cost control. Hard, in code, checked before each call."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings):
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

    @property
    def total_cents(self) -> int:
        return self.already_spent_cents + round(self.this_run_usd * 100)

    @property
    def reached(self) -> bool:
        return self.total_cents >= self.cap_cents

    def charge(self, usd: float) -> None:
        self.this_run_usd += usd


def sync(
    conn: sqlite3.Connection,
    settings: Settings,
    connectors: list[Connector],
    client: ModelClient,
    *,
    dry_run: bool = False,
) -> SyncReport:
    report = SyncReport()
    ledger = Ledger(conn, settings, dry_run=dry_run)
    cap = SpendCap(conn, settings)
    started_at = now_iso()

    _ingest(conn, ledger, connectors, report, dry_run=dry_run)
    _rule_pass(conn, ledger, settings, report)
    _triage_pass(conn, ledger, settings, client, cap, report)
    _extract_pass(conn, ledger, settings, client, cap, report)

    # Review-day tallies → checkpoints. Deterministic — the data arrives structured,
    # so this is code, not a model call, and it costs nothing on the cap. Skipped on
    # dry-run like every other write.
    review_writes = 0
    if not dry_run:
        from backglass.goals import reviews as reviews_mod

        review_writes = reviews_mod.sync_checkpoints(conn, settings)

    report.writes = ledger.writes + review_writes
    report.spend_cents = round(cap.this_run_usd * 100)
    report.degraded = cap.reached
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
        except Exception as exc:  # noqa: BLE001 - rule 5: degrade, never block
            detail = f"{type(exc).__name__}: {exc}"[:300]
            report.failed_sources.append(connector.name)
            report.errors.append(f"{connector.name}: {detail}")
            if not dry_run:
                credentials.mark_failed(conn, connector.name, detail)
            continue

        report.excluded += getattr(connector, "excluded", 0)
        for rule, count in (getattr(connector, "excluded_by_rule", None) or {}).items():
            report.excluded_by_rule[rule] = report.excluded_by_rule.get(rule, 0) + count


# ──────────────────────────────────────────────────────────────── stage 2


def _rule_pass(
    conn: sqlite3.Connection, ledger: Ledger, settings: Settings, report: SyncReport
) -> None:
    """Tier 0. Free, deterministic, and it is what keeps the model bill small."""
    noise = frozenset(settings.noise_senders)
    pending = list(conn.execute(query("pending_triage"), {"user_id": USER_ID}))
    for item in pending:
        headers = _headers_of(item)
        verdict = rules.classify(
            headers=headers,
            author=str(item.get("author") or ""),
            raw_json=str(item.get("raw_json") or ""),
            noise_senders=noise,
        )
        if verdict.dropped:
            ledger.record_triage(int(item["id"]), "drop", verdict.reason)
            report.rule_dropped += 1
            report.triaged_out += 1


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

    for item_id, outcome in _in_parallel(
        work, pending, settings.max_concurrency, cap, stop_on_cap=False
    ):
        if isinstance(outcome, Exception):
            report.errors.append(f"triage {item_id}: {outcome}")
            continue
        cap.charge(outcome.cost_usd)
        ledger.record_triage(item_id, outcome.verdict, outcome.reason)
        report.model_triaged += 1
        if outcome.verdict == "drop":
            report.triaged_out += 1


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
    pending = list(
        conn.execute(
            query("pending_extraction"),
            {"user_id": USER_ID, "extraction_version": prompt.stamp},
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
            )
        except Exception as exc:  # noqa: BLE001
            return item, exc

    for item, outcome in _in_parallel(
        work, pending, settings.max_concurrency, cap, stop_on_cap=True
    ):
        item_id = int(item["id"])
        if isinstance(outcome, Exception):
            # extract-commitments.md §Failure handling: an item that fails is parked with
            # extraction_version unset, so a later prompt version retries it, and it is
            # surfaced rather than silently skipped.
            report.parked += 1
            report.errors.append(f"extract {item_id}: {outcome}")
            continue
        extraction, cost = outcome
        cap.charge(float(cost))
        applied = tier2.apply(
            extraction,
            source_item_id=item_id,
            occurred_at=str(item["occurred_at"]),
            ledger=ledger,
            settings=settings,
        )
        ledger.record_extraction_version(item_id, prompt.stamp)
        report.extracted += 1
        report.review_queue += applied.review_queue
        report.date_notes.extend(applied.date_notes)


# ──────────────────────────────────────────────────────────────── helpers


def _in_parallel[T, R](
    work: Callable[[T], R],
    items: Sequence[T],
    max_workers: int,
    cap: SpendCap,
    *,
    stop_on_cap: bool,
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
    """
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = []
        for item in items:
            if stop_on_cap and cap.reached:
                break
            futures.append(pool.submit(work, item))
        for future in futures:
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


def _record_run(conn: sqlite3.Connection, report: SyncReport, started_at: str) -> None:
    import json

    conn.execute(
        "INSERT INTO run (user_id, started_at, finished_at, items_fetched, items_triaged_out, "
        " items_excluded, items_extracted, writes, spend_cents, degraded, errors_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            USER_ID,
            started_at,
            now_iso(),
            report.fetched,
            report.triaged_out,
            report.excluded,
            report.extracted,
            report.writes,
            report.spend_cents,
            1 if report.degraded else 0,
            json.dumps(report.errors) if report.errors else None,
        ),
    )
