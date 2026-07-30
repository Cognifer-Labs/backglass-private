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
from backglass.extract.schemas import TriageVerdict, json_schema

BODY_LIMIT = 2000
SCHEMA = json_schema(TriageVerdict)


@dataclass(frozen=True)
class TriageOutcome:
    verdict: str  # 'keep' | 'drop'
    reason: str
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
    rendered = prompt.render(
        author=item.get("author") or "",
        occurred_at=item.get("occurred_at") or "",
        title=item.get("title") or "",
        body_text_truncated_2000=body[:BODY_LIMIT],
    )
    result = client.complete(
        system=SYSTEM, user=rendered, schema=SCHEMA, model=model, budget_usd=budget_usd
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
