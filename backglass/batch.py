"""Overnight extraction through the Message Batches API, at half price.

Design decision: **triage runs synchronously, only extraction is batched.** The money
is in sonnet extraction (rules and cheap triage kill 90-95% of volume first), and
running triage inline at submit time means the extraction batch is fully determined
in one pass — one batch, one poll, one failure surface. The rejected alternative
(batch triage too, collect it, then submit extraction) doubles item latency to 48h
and turns the pre-brief collect job into a submitter with its own failure modes at
the worst time of day.

Flow (launchd/README.md has the schedule):
  22:00  `backglass batch submit`  — sync(extract=False), package pending extraction
  05:30  `backglass batch collect` — apply results through the same ledger paths sync
         uses, charge the discounted actuals, write a run row

Failure policy is CLAUDE.md rule 5 end to end: an expired or errored batch leaves its
items pending (`extraction_version` NULL), and the next scheduled sync extracts them
synchronously at full price. Nothing ever blocks the brief.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from backglass.config import Settings
from backglass.connectors.base import Connector
from backglass.db import now_iso, query
from backglass.extract import commitments as tier2
from backglass.extract import engagements as engagement_tier2
from backglass.extract import pricing, prompts
from backglass.extract.client import ModelClient, anthropic_api_key, build_request
from backglass.extract.schemas import CommitmentExtraction, json_schema
from backglass.ledger import USER_ID, Ledger
from backglass.sync import EXTRACT_PROMPT, SpendCap, record_run, sync

#: Batches complete "usually within 1 hour, max 24" — past this window a batch is
#: presumed dead and its items fall back to the synchronous path automatically.
EXPIRY_HOURS = 26
#: The Batches API bills at 50% of live prices.
DISCOUNT = 0.5


@dataclass
class SubmitReport:
    fetched: int = 0
    triaged: int = 0
    batched: int = 0
    skipped_oversize: int = 0
    skipped_for_cap: int = 0
    batch_id: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0


@dataclass
class CollectReport:
    batches: int = 0
    still_processing: int = 0
    extracted: int = 0
    already_done: int = 0
    failed_items: int = 0
    review_queue: int = 0
    spend_cents: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0


def _real_client(settings: Settings) -> Any:
    import anthropic

    key = anthropic_api_key(settings)
    if not key:
        raise RuntimeError(
            "batch mode needs the Anthropic API: set MODEL_API_KEY or ANTHROPIC_API_KEY"
        )
    return anthropic.Anthropic(api_key=key, max_retries=4)


def submit(
    conn: sqlite3.Connection,
    settings: Settings,
    connectors: list[Connector],
    client: ModelClient,
    anthropic_client: Any | None = None,
) -> SubmitReport:
    report = SubmitReport()

    # Stages 1-3 through the existing pipeline — same ledger paths, same idempotency,
    # same SpendCap, same run row. Only extraction is withheld.
    sync_report = sync(conn, settings, connectors, client, extract=False)
    report.fetched = sync_report.fetched
    report.triaged = sync_report.model_triaged
    report.errors.extend(sync_report.errors)

    prompt = prompts.load(EXTRACT_PROMPT)
    cutoff = (datetime.now(UTC) - timedelta(hours=EXPIRY_HOURS)).isoformat()
    pending = list(
        conn.execute(
            query("pending_extraction_unbatched"),
            {"user_id": USER_ID, "extraction_version": prompt.stamp, "cutoff": cutoff},
        )
    )

    # Oversized items are not batched; the sync path parks them with a visible error,
    # and hiding that inside a batch would bury it.
    eligible = []
    for item in pending:
        if len(str(item.get("body_text") or "")) > settings.per_item_char_ceiling:
            report.skipped_oversize += 1
            continue
        eligible.append(item)

    cap = SpendCap(conn, settings)
    if cap.reached:
        report.errors.append(
            f"spend cap reached ({cap.total_cents}c of {cap.cap_cents}c); "
            f"nothing submitted, {len(eligible)} item(s) left pending"
        )
        return report
    # Bound the batch by remaining budget at a conservative per-item ceiling: the
    # discounted per-call budget. Oldest first, same order sync extracts in.
    est_item_cents = max(1, round(settings.per_call_budget_usd * 100 * DISCOUNT))
    max_items = (cap.cap_cents - cap.total_cents) // est_item_cents
    if len(eligible) > max_items:
        report.skipped_for_cap = len(eligible) - max_items
        eligible = eligible[:max_items]

    if not eligible:
        return report

    model_id = pricing.resolve(settings.model_extract)
    schema = json_schema(CommitmentExtraction)
    requests = []
    for item in eligible:
        system, user = tier2.render_parts(
            dict(item),
            prompt=prompt,
            settings=settings,
            # The batch path renders the same prompt as the live one, so it needs the
            # same context or an overnight run would read every reply blind.
            context=tier2.conversation_context(conn, int(item["id"])),
        )
        requests.append(
            {
                "custom_id": f"si-{item['id']}",
                "params": build_request(
                    system=system,
                    user=user,
                    schema=schema,
                    model_id=model_id,
                    max_tokens=4096,
                ),
            }
        )

    api = anthropic_client or _real_client(settings)
    batch = api.messages.batches.create(requests=requests)

    conn.execute(
        "INSERT INTO model_batch (user_id, batch_id, kind, model, prompt_stamp,"
        " status, created_at) VALUES (?, ?, 'extract', ?, ?, 'submitted', ?)",
        (USER_ID, batch.id, model_id, prompt.stamp, now_iso()),
    )
    conn.executemany(
        "INSERT INTO model_batch_item (batch_id, custom_id, source_item_id)"
        " VALUES (?, ?, ?)",
        [(batch.id, f"si-{item['id']}", int(item["id"])) for item in eligible],
    )
    report.batched = len(eligible)
    report.batch_id = str(batch.id)
    return report


def collect(
    conn: sqlite3.Connection,
    settings: Settings,
    anthropic_client: Any | None = None,
) -> CollectReport:
    report = CollectReport()
    started_at = now_iso()
    outstanding = list(
        conn.execute(
            "SELECT batch_id, model, prompt_stamp, created_at FROM model_batch"
            " WHERE user_id = ? AND status = 'submitted'",
            (USER_ID,),
        )
    )
    if not outstanding:
        return report

    api = anthropic_client or _real_client(settings)
    cap = SpendCap(conn, settings)
    ledger = Ledger(conn, settings)
    spend_usd = 0.0
    extracted = 0

    for row in outstanding:
        batch_id = str(row["batch_id"])
        batch_usd = 0.0  # this batch alone; spend_usd is the run total
        try:
            remote = api.messages.batches.retrieve(batch_id)
        except Exception as exc:  # noqa: BLE001 - rule 5: degrade, never block
            report.errors.append(f"batch {batch_id}: retrieve failed: {exc}")
            continue

        if getattr(remote, "processing_status", "") != "ended":
            expired = str(row["created_at"]) < (
                datetime.now(UTC) - timedelta(hours=EXPIRY_HOURS)
            ).isoformat()
            if expired:
                # Items were never stamped, so the next sync extracts them
                # synchronously — the valve needs no operator.
                conn.execute(
                    "UPDATE model_batch SET status = 'expired', error = ?"
                    " WHERE batch_id = ?",
                    (f"not ended after {EXPIRY_HOURS}h; items fall back to sync", batch_id),
                )
                conn.execute(
                    "UPDATE model_batch_item SET status = 'expired'"
                    " WHERE batch_id = ? AND status = 'pending'",
                    (batch_id,),
                )
                report.errors.append(f"batch {batch_id}: expired; items back to sync")
            else:
                report.still_processing += 1
            continue

        report.batches += 1
        mapping = {
            str(r["custom_id"]): int(r["source_item_id"])
            for r in conn.execute(
                "SELECT custom_id, source_item_id FROM model_batch_item"
                " WHERE batch_id = ?",
                (batch_id,),
            )
        }

        # Results arrive in any order; supersession depends on application order, so
        # gather first, then apply oldest-occurred first — exactly like the sync path.
        succeeded: list[tuple[str, int, Any]] = []
        for result in api.messages.batches.results(batch_id):
            custom_id = str(result.custom_id)
            item_id = mapping.get(custom_id)
            if item_id is None:
                continue  # an id we never sent proves nothing
            kind = getattr(result.result, "type", "errored")
            if kind == "succeeded":
                succeeded.append((custom_id, item_id, result.result.message))
            else:
                conn.execute(
                    "UPDATE model_batch_item SET status = ? WHERE batch_id = ?"
                    " AND custom_id = ?",
                    ("expired" if kind == "expired" else "errored", batch_id, custom_id),
                )
                report.failed_items += 1
                report.errors.append(
                    f"batch item {custom_id}: {kind}; falls back to the next sync"
                )

        def _occurred(entry: tuple[str, int, Any]) -> str:
            # datetime() normalizes to UTC before we sort. The raw column keeps each
            # source's own offset, so sorting it as text puts a 23:50 Phoenix message
            # (06:50Z next day) *before* a 00:10 Kolkata one (18:40Z the day before) —
            # and supersession depends on applying oldest-first, so the inversion
            # leaves a resolved commitment open.
            found = conn.execute(
                "SELECT datetime(occurred_at) AS at FROM source_item WHERE id = ?",
                (entry[1],),
            ).fetchone()
            return str(found["at"]) if found and found["at"] else ""

        for custom_id, item_id, message in sorted(succeeded, key=_occurred):
            batch_usd += pricing.cost_usd(str(row["model"]), message.usage) * DISCOUNT
            current = conn.execute(
                "SELECT occurred_at, extraction_version FROM source_item WHERE id = ?",
                (item_id,),
            ).fetchone()
            if current is None:
                continue
            if current["extraction_version"] == str(row["prompt_stamp"]):
                # An interleaved sync (or a prior collect that crashed mid-way) got
                # here first — idempotency by stored state, same as sync's.
                report.already_done += 1
                conn.execute(
                    "UPDATE model_batch_item SET status = 'succeeded'"
                    " WHERE batch_id = ? AND custom_id = ?",
                    (batch_id, custom_id),
                )
                continue
            block = next(
                (b for b in message.content if getattr(b, "type", "") == "tool_use"),
                None,
            )
            data = getattr(block, "input", None)
            try:
                extraction = CommitmentExtraction.model_validate(data)
            except Exception as exc:  # noqa: BLE001 - pydantic's own error type
                report.failed_items += 1
                report.errors.append(f"batch item {custom_id}: unusable output: {exc}")
                conn.execute(
                    "UPDATE model_batch_item SET status = 'errored'"
                    " WHERE batch_id = ? AND custom_id = ?",
                    (batch_id, custom_id),
                )
                continue
            # One transaction per item, matching sync.py's extract pass: the commitment
            # rows, the extraction stamp and the item's 'succeeded' mark are one fact.
            # Autocommit would let a crash between them leave rows written against an
            # item still marked in-flight, and the next collect would apply the same
            # result a second time.
            conn.execute("BEGIN IMMEDIATE")
            try:
                applied = tier2.apply(
                    extraction,
                    source_item_id=item_id,
                    occurred_at=str(current["occurred_at"]),
                    ledger=ledger,
                    settings=settings,
                )
                # Batched and live extraction read the same response through the same
                # two appliers, so a plan found overnight is the same row as one found
                # on demand. tests/test_connectors.py's drift rule applies here too.
                plans = engagement_tier2.apply(
                    extraction,
                    source_item_id=item_id,
                    occurred_at=str(current["occurred_at"]),
                    ledger=ledger,
                    settings=settings,
                )
                ledger.record_extraction_version(item_id, str(row["prompt_stamp"]))
                conn.execute(
                    "UPDATE model_batch_item SET status = 'succeeded'"
                    " WHERE batch_id = ? AND custom_id = ?",
                    (batch_id, custom_id),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            extracted += 1
            report.review_queue += applied.review_queue + plans.review_queue

        cap.charge(batch_usd)
        spend_usd += batch_usd
        conn.execute(
            "UPDATE model_batch SET status = 'collected', collected_at = ?,"
            " spend_cents = ? WHERE batch_id = ?",
            (now_iso(), round(batch_usd * 100), batch_id),
        )

    report.extracted = extracted
    report.spend_cents = round(spend_usd * 100)
    if report.batches or report.errors:
        record_run(
            conn,
            started_at=started_at,
            extracted=extracted,
            writes=ledger.writes,
            spend_cents=report.spend_cents,
            errors=report.errors or None,
        )
    return report
