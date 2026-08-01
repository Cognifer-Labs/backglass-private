"""Tier 1: the triage model. specs/extraction-prompts/triage.md.

One binary question, nothing else. Every token spent here multiplies across the whole
inbox, which is why the body is truncated to 2000 characters and why the prompt is the
shortest one in the system.

The asymmetry in the uncertainty instruction — when in doubt, keep — is deliberate and
triage.md says not to tune it away to save money. The spend cap in sync.py is the
mechanism for cost control; degrading triage precision is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backglass.extract.client import SYSTEM, ModelClient, ModelError
from backglass.extract.prompts import Prompt
from backglass.extract.schemas import TriageBatchVerdict, TriageVerdict, json_schema

BODY_LIMIT = 2000
#: Batch excerpts are short on purpose: the batch pass only ever *confidently* drops;
#: anything it hedges about is re-read at the full BODY_LIMIT by the per-item pass.
BATCH_BODY_LIMIT = 500
SCHEMA = json_schema(TriageVerdict)
BATCH_SCHEMA = json_schema(TriageBatchVerdict)


@dataclass(frozen=True)
class TriageOutcome:
    verdict: str  # 'keep' | 'drop'
    reason: str
    cost_usd: float


@dataclass(frozen=True)
class BatchOutcome:
    """Confident verdicts plus the ids that must be re-read per-item.

    `escalate` collects everything the batch cannot be trusted on: items the model
    marked uncertain, ids missing from the response, and ids returned more than once
    with disagreeing verdicts. Unknown ids in the response are simply ignored.
    """

    verdicts: dict[int, TriageOutcome]
    escalate: set[int]
    cost_usd: float


def triage(
    item: dict[str, Any],
    *,
    prompt: Prompt,
    client: ModelClient,
    model: str,
    budget_usd: float,
) -> TriageOutcome:
    body = str(item.get("body_text") or "")
    # The static instruction half rides in `system` so a caching backend can bill it
    # once (prompts.Prompt.split). Total content and order are unchanged for every
    # backend — system precedes user everywhere — so no prompt version bump.
    static, _ = prompt.split()
    system = f"{SYSTEM}\n\n{static}" if static else SYSTEM
    rendered = prompt.render_dynamic(
        author=item.get("author") or "",
        occurred_at=item.get("occurred_at") or "",
        title=item.get("title") or "",
        body_text_truncated_2000=body[:BODY_LIMIT],
    )
    result = client.complete(
        system=system, user=rendered, schema=SCHEMA, model=model, budget_usd=budget_usd
    )
    try:
        parsed = TriageVerdict.model_validate(result.data)
    except Exception as exc:  # noqa: BLE001 - pydantic raises its own error type
        raise ModelError(f"triage response did not match the schema: {exc}") from exc
    return TriageOutcome(
        verdict="keep" if parsed.keep else "drop",
        reason=parsed.reason,
        cost_usd=result.cost_usd,
    )


def _render_batch_item(item: dict[str, Any]) -> str:
    body = str(item.get("body_text") or "")[:BATCH_BODY_LIMIT]
    return (
        f"--- id: {item['id']}\n"
        f"From: {item.get('author') or ''}\n"
        f"Date: {item.get('occurred_at') or ''}\n"
        f"Subject: {item.get('title') or ''}\n\n"
        f"{body}"
    )


def triage_batch(
    items: list[dict[str, Any]],
    *,
    prompt: Prompt,
    client: ModelClient,
    model: str,
    budget_usd: float,
) -> BatchOutcome:
    """One model call over many items. Instruction tokens are paid once.

    Raises ModelError like `triage` does; the caller treats a failed batch as
    every-item-escalates (degrade, never block).
    """
    sent = {int(item["id"]) for item in items}
    static, _ = prompt.split()
    system = f"{SYSTEM}\n\n{static}" if static else SYSTEM
    rendered = prompt.render_dynamic(
        items="\n\n".join(_render_batch_item(i) for i in items)
    )
    result = client.complete(
        system=system, user=rendered, schema=BATCH_SCHEMA, model=model,
        budget_usd=budget_usd,
    )
    try:
        parsed = TriageBatchVerdict.model_validate(result.data)
    except Exception as exc:  # noqa: BLE001 - pydantic raises its own error type
        raise ModelError(f"batch triage response did not match the schema: {exc}") from exc

    verdicts: dict[int, TriageOutcome] = {}
    escalate: set[int] = set()
    for entry in parsed.items:
        if entry.id not in sent:
            continue  # an id we never sent proves nothing about anything we did
        if entry.uncertain:
            escalate.add(entry.id)
            continue
        outcome = TriageOutcome(
            verdict="keep" if entry.keep else "drop",
            reason=entry.reason,
            cost_usd=0.0,
        )
        previous = verdicts.get(entry.id)
        if previous is not None and previous.verdict != outcome.verdict:
            escalate.add(entry.id)  # returned twice, disagreeing — trust neither
            continue
        verdicts[entry.id] = outcome
    for item_id in escalate:
        verdicts.pop(item_id, None)
    escalate.update(sent - set(verdicts) - escalate)
    return BatchOutcome(verdicts=verdicts, escalate=escalate, cost_usd=result.cost_usd)
