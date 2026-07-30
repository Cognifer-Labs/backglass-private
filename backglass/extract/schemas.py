"""Pydantic v2 models are the only definition of shape in the codebase.

docs/10 §Model layer: "Generate the tool JSON Schema from the Pydantic model so the two
cannot drift." `json_schema()` below is that generator; nothing hand-writes a schema.

The shapes mirror the `## Output schema` blocks in specs/extraction-prompts/. If you
change one, change the other in the same commit — the prompt is documentation for the
model, this file is enforcement.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TriageVerdict(Strict):
    """specs/extraction-prompts/triage.md §Output schema."""

    keep: bool
    #: Required, and must quote or paraphrase the specific trigger. triage.md: "It is
    #: what makes triage tunable: reading a week of reasons is how you find the rule that
    #: should have caught something before it reached the model."
    reason: str


class ExtractedCommitment(Strict):
    """One commitment as the model returns it, before post-processing.

    Deliberately not the same shape as the `commitment` table. The model does not know
    about entity IDs, statuses, or estimate sources — those are assigned in code by
    extract-commitments.md §Post-processing.
    """

    direction: Literal["i_owe", "owed_to_me"]
    counterparty: str | None = None
    what: str
    due_at: str | None = None
    due_is_explicit: bool = False
    estimated_minutes: int | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    #: The exact sentence, verbatim. CLAUDE.md rule 1: a claim with no provenance does
    #: not ship, and this is the provenance at the sentence level.
    evidence: str
    resolves: bool = False
    resolves_what: str | None = None


class CommitmentExtraction(Strict):
    """An empty list is a valid and common result. Do not invent one to be useful."""

    commitments: list[ExtractedCommitment] = Field(default_factory=list)


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A self-contained JSON Schema for `--json-schema` / a tool `input_schema`.

    Pydantic emits `$ref`/`$defs` for nested models. Both the Claude CLI and DeepInfra
    accept that, but inlining keeps the schema readable in a debug log and removes a
    class of provider-specific `$ref` resolution differences, which is worth more than
    the duplication costs.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    inlined = _inline(schema, defs)
    assert isinstance(inlined, dict)
    return inlined


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.rsplit("/", 1)[1], {})
            merged = {**_inline(target, defs), **{k: v for k, v in node.items() if k != "$ref"}}
            return merged
        return {key: _inline(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    return node
