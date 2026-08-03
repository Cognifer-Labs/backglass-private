"""Tier 2: commitment extraction, plus the post-processing that turns a model response
into ledger rows.

specs/extraction-prompts/extract-commitments.md §Post-processing is explicit that steps 1
through 5 are code, not prompt. They are the five functions below `apply()`. The reason
they are not in the prompt is that they are deterministic and the prompt is not: entity
resolution, dedup and supersession must give the same answer twice for the same input,
which is what CLAUDE.md rule 3 requires of the whole sync.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from backglass.config import Settings
from backglass.extract import dates, entities
from backglass.extract.client import SYSTEM, ModelClient, ModelError
from backglass.extract.prompts import Prompt
from backglass.extract.schemas import CommitmentExtraction, ExtractedCommitment, json_schema
from backglass.ledger import Ledger

SCHEMA = json_schema(CommitmentExtraction)


@dataclass
class ApplyReport:
    inserted: int = 0
    deduped: int = 0
    superseded: int = 0
    review_queue: int = 0
    date_notes: list[str] = field(default_factory=list)


def render_parts(
    item: dict[str, Any], *, prompt: Prompt, settings: Settings
) -> tuple[str, str]:
    """(system, user) for one item — the static instruction prefix rides in `system`
    for prompt caching (prompts.Prompt.split). Shared with the batch path
    (backglass/batch.py) so live and batched extraction cannot drift.

    The owner line early in the prompt file shortens the cacheable prefix — accepted,
    because restructuring the file would bump its version and re-extract everything.
    """
    static, _ = prompt.split()
    system = f"{SYSTEM}\n\n{static}" if static else SYSTEM
    rendered = prompt.render_dynamic(
        owner_name=settings.owner_name,
        # The prompt spec is written for a single address. The owner has two, and
        # direction is decided against both. Rendered as a list rather than editing a
        # versioned prompt file, which would force a version bump and re-extraction.
        owner_email=", ".join(settings.owner_emails),
        author=item.get("author") or "",
        recipients=_recipients(item.get("raw_json")),
        occurred_at=item.get("occurred_at") or "",
        title=item.get("title") or "",
        body_text=item.get("body_text") or "",
    )
    return system, rendered


def extract(
    item: dict[str, Any],
    *,
    prompt: Prompt,
    client: ModelClient,
    model: str,
    budget_usd: float,
    settings: Settings,
) -> tuple[CommitmentExtraction, float]:
    system, rendered = render_parts(item, prompt=prompt, settings=settings)
    result = client.complete(
        system=system, user=rendered, schema=SCHEMA, model=model, budget_usd=budget_usd
    )
    try:
        parsed = CommitmentExtraction.model_validate(result.data)
    except Exception as exc:  # noqa: BLE001 - pydantic raises its own error type
        raise ModelError(f"extraction response did not match the schema: {exc}") from exc
    return parsed, result.cost_usd


def apply(
    extraction: CommitmentExtraction,
    *,
    source_item_id: int,
    occurred_at: str,
    ledger: Ledger,
    settings: Settings,
) -> ApplyReport:
    """Post-processing steps 1–5, in order."""
    report = ApplyReport()
    accepted_in_this_message: list[tuple[str, int | None, str, int]] = []

    for candidate in extraction.commitments:
        # ── step 1: resolve the counterparty to an entity, create on miss
        entity_id = ledger.resolve_entity(candidate.counterparty)

        # ── date resolution (CLAUDE.md rule 4) before anything is stored
        resolved = dates.resolve_due(candidate.due_at, occurred_at=occurred_at)
        if resolved.note:
            report.date_notes.append(f"{candidate.what!r}: {resolved.note}")

        # ── step 5: dedup, both against the ledger and within this one response.
        #
        # Skipped when the message resolves an earlier commitment. A resolving message is
        # by definition about the same thing as the commitment it resolves — "here's that
        # plan I promised" describes the promised plan — so dedup would always match, the
        # candidate would be dropped, and step 4 below would never run. The original
        # commitment would then stay `open` forever, which is precisely the bug the
        # supersede path exists to prevent.
        if not candidate.resolves:
            duplicate_of = _duplicate_of(
                candidate, entity_id, ledger, settings, accepted_in_this_message
            )
            if duplicate_of is not None:
                # The restatement is a second sighting of the same obligation, not noise.
                # Dropping the row was always right; dropping the *sentence* meant a
                # thread that repeated a promise four times left one citation, and the
                # owner had no way to see that it had been said again since. Recorded
                # against the commitment it matched, deduplicated on (commitment, item)
                # so re-reading the same message writes nothing.
                ledger.record_evidence(
                    duplicate_of, source_item_id, candidate.evidence, kind="restated"
                )
                report.deduped += 1
                ledger.stats.commitments_deduped += 1
                continue

        # ── step 2: estimated_minutes.
        # The type-default table lives in docs/04 §capacity, which is Phase 4, and nothing
        # reads this column before then. Phase 1 records only what the model actually
        # supported. See tasks/todo.md §Deviations #5.
        estimate_source = "extracted" if candidate.estimated_minutes is not None else None

        new_id = ledger.insert_commitment(
            direction=candidate.direction,
            entity_id=entity_id,
            what=candidate.what,
            due_at=resolved.value,
            estimated_minutes=candidate.estimated_minutes,
            estimate_source=estimate_source,
            confidence=candidate.confidence,
            source_item_id=source_item_id,
            evidence=candidate.evidence,
        )
        report.inserted += 1
        accepted_in_this_message.append(
            (candidate.direction, entity_id, candidate.what, new_id)
        )

        # ── step 3: below threshold goes to the review queue, never into the brief.
        # The row is still created with status 'open'; what changes is who reads it.
        # db/queries/open_commitments.sql computes needs_review from the same threshold.
        if candidate.confidence < settings.confidence_threshold:
            report.review_queue += 1

        # ── step 4: supersede what this message resolves. Never delete (docs/03).
        #
        # Gated on confidence, which step 3 above applies to *display* and this branch
        # did not apply at all. Closing a commitment is a heavier act than showing one:
        # a wrong insert is visible on the board and can be dropped, while a wrong
        # supersede makes a real obligation silently disappear from every surface.
        #
        # It is also the one write reachable by a hostile sender. The extraction prompt
        # ends with the message body, so "sent it last night, you're all set" is read as
        # instructions; with no counterparty the NULL-safe match in
        # open_commitments_for_dedup.sql lands on exactly the commitments the owner
        # typed by hand. An unconfirmed resolution now waits in the review queue, where
        # a human decides, instead of closing the row on a stranger's say-so.
        if candidate.resolves and candidate.resolves_what:
            if candidate.confidence < settings.confidence_threshold:
                report.review_queue += 1
            else:
                superseded = _find_resolved(candidate, entity_id, ledger, settings)
                if superseded is not None and superseded != new_id:
                    ledger.supersede(superseded, new_id)
                    report.superseded += 1

    return report


# ────────────────────────────────────────────────────────────────── helpers


def _duplicate_of(
    candidate: ExtractedCommitment,
    entity_id: int | None,
    ledger: Ledger,
    settings: Settings,
    accepted: list[tuple[str, int | None, str, int]],
) -> int | None:
    """Step 5. "A thread restating the same promise must not produce five rows."

    Returns the id of the commitment this candidate restates, so the caller can cite the
    restatement against it. It used to return a bare bool, which threw away the one fact
    the dedup pass had just established — which existing row this sentence is about.

    Two passes, because a restatement can be either already in the ledger from an earlier
    message in the thread, or repeated twice inside one message.
    """
    for direction, other_entity, what, commitment_id in accepted:
        if (
            direction == candidate.direction
            and other_entity == entity_id
            and entities.similar(what, candidate.what) >= settings.dedup_threshold
        ):
            return commitment_id

    for existing in ledger.open_commitments_for(candidate.direction, entity_id):
        if entities.similar(str(existing["what"]), candidate.what) >= settings.dedup_threshold:
            return int(existing["id"])
    return None


def _find_resolved(
    candidate: ExtractedCommitment,
    entity_id: int | None,
    ledger: Ledger,
    settings: Settings,
) -> int | None:
    """Step 4. Find the open commitment this message resolves.

    Searched in both directions: "here's that plan I promised" resolves an `i_owe`, while
    "thanks, received" resolves an `owed_to_me`, and the model reports `resolves` on
    whichever row it is describing.
    """
    target = candidate.resolves_what or ""
    best: tuple[float, int] | None = None
    for direction in ("i_owe", "owed_to_me"):
        for existing in ledger.open_commitments_for(direction, entity_id):
            score = entities.similar(str(existing["what"]), target)
            if score >= settings.dedup_threshold and (best is None or score > best[0]):
                best = (score, int(existing["id"]))
    return best[1] if best else None


def _recipients(raw_json: object) -> str:
    if not raw_json:
        return ""
    try:
        parsed = json.loads(str(raw_json))
    except (ValueError, TypeError):
        return ""
    headers = parsed.get("headers") if isinstance(parsed, dict) else None
    if not isinstance(headers, dict):
        return ""
    values = [str(v) for k, v in headers.items() if k.lower() in ("to", "cc")]
    return ", ".join(v for v in values if v)
