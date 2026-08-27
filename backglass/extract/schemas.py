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


class TriageBatchItem(Strict):
    """One verdict inside a batched triage response (triage-batch.md)."""

    id: int
    keep: bool
    reason: str
    #: True when the 500-char excerpt was not enough to judge confidently. The item is
    #: then re-read by the full per-item pass; this verdict is advisory only.
    uncertain: bool = False


class TriageBatchVerdict(Strict):
    """specs/extraction-prompts/triage-batch.md §Output schema."""

    items: list[TriageBatchItem] = Field(default_factory=list)


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


class ExtractedEngagement(Strict):
    """A plan to be somewhere with someone, as the model returns it.

    Distinct from a commitment because the question it answers is different: a
    commitment asks what is still owed, an engagement asks who the owner is seeing and
    whether they have answered yet. See migration 0014 for why they are separate rows.
    """

    kind: Literal["social", "professional"]
    what: str
    #: Names or emails as written. Plural because "drinks with Priya and Sam" is one
    #: plan, and asking "when did I last see Sam" has to find it through either name.
    people: list[str] = Field(default_factory=list)
    #: Null is meaningful, not missing: "we should get dinner sometime" is a real plan
    #: with no time, and it is exactly the kind that goes stale unanswered.
    starts_at: str | None = None
    ends_at: str | None = None
    when_is_explicit: bool = False
    location: str | None = None
    #: Someone suggesting dinner is not dinner. Only `confirmed` earns a day-plan block.
    status: Literal["proposed", "confirmed", "declined"] = "proposed"
    confidence: float = Field(ge=0.0, le=1.0)
    #: True when the message is MOVING a plan that was already arranged ("push dinner to
    #: 7:30", "let's do Saturday instead") rather than proposing a new one.
    #:
    #: The field exists because the data cannot answer the question and four rounds of
    #: heuristics proved it. Matching a restatement on its clock time turns every
    #: reschedule into a second row that double-books the day; matching on the day alone
    #: lets a 4pm plan repaint an unrelated 9am one and destroys it. "Coffee at 4" after
    #: "coffee at 9" is a different coffee or the same coffee moved, and only the sentence
    #: knows which. This is the same admission `resolves` makes for commitments.
    replaces_earlier: bool = False
    #: When `replaces_earlier` is set, the time the plan had BEFORE this message moves it
    #: — "Friday's dinner has to move" gives Friday. Null when the message does not say.
    #:
    #: The flag alone is not enough to act on, which cost a verification round to learn: a
    #: move is identified by what it moves FROM, and its new time is by construction near
    #: where it is GOING. Matching on the new time picked whichever unrelated plan already
    #: sat near the destination and repainted that instead. With no stated old time the
    #: move cannot be aimed, so it becomes a new plan — visible, and dismissible — rather
    #: than a silent overwrite of the wrong one. Mirrors `resolves_what` above.
    replaces_start_at: str | None = None
    #: Same rule as the commitment above — rule 1 applies to every generated claim.
    evidence: str


class ExtractedFact(Strict):
    """A durable claim about the owner's life, as the model returns it.

    The knowledge base was hand-typed only until v10: a pipeline that had read nine
    thousand items had contributed zero facts (state's `with_provenance: 0`). This is
    the writeback — but a fact feeds `owner_context`, which feeds every future model
    call, so unlike a commitment a wrong fact COMPOUNDS. The citation gate in
    facts.apply_extracted decides whether a candidate may become active on its own or
    waits as `proposed` for the owner; the model only ever nominates.
    """

    #: Kebab lane, from the closed list in the prompt (identity, education, housing,
    #: preferences, people, family, work, health, orgtruth, other). Closed because a
    #: model inventing lanes forks the knowledge base into near-duplicates no reader
    #: groups together.
    subject: str
    key: str
    value: str
    #: The exact sentence, verbatim — rule 1 at the sentence level, and what the
    #: citation gate verifies against the body before anything lands active.
    evidence: str
    confidence: float = Field(ge=0.0, le=1.0)


class CommitmentExtraction(Strict):
    """One tier-2 read produces both record types.

    Deliberately one response and not two calls: docs/02's two-tier design spends model
    money once per item, and a second careful pass over the same text to ask a second
    question would double extraction cost for every message the owner ever receives.
    The name is now narrower than the contents; it is kept because it is what
    `batch.py`, the CLI and the fixtures all refer to, and renaming it would churn more
    than it clarifies.
    """

    commitments: list[ExtractedCommitment] = Field(default_factory=list)
    engagements: list[ExtractedEngagement] = Field(default_factory=list)
    #: v10: durable facts, same call — a third question over the same text for the
    #: same one-read price. Old (v9) responses simply have none; the default keeps
    #: every stored fixture and batch reply parseable.
    facts: list[ExtractedFact] = Field(default_factory=list)


class RecheckVerdict(Strict):
    """One re-read of one open commitment against the conversation that followed it.

    `commitment_id` is the ledger id the pass handed the model, returned so the verdict
    needs no similarity matcher — and intersected with the set actually sent before
    anything is written, because every id in this codebase is a bare int and the tables
    overlap in range (tasks/lessons.md 2026-08-12).

    `cites` and `quote` are what separate this from guessing. A `done` or `dropped`
    verdict without both is discarded rather than stored: silence is not evidence, and
    "nobody mentioned it again" would close every obligation the owner has been quietly
    failing to do.
    """

    commitment_id: int
    verdict: Literal["done", "dropped", "open"]
    confidence: float = Field(ge=0.0, le=1.0)
    #: `source_item.id` of the message that settles it. None is only valid for `open`.
    cites: int | None = None
    #: Verbatim, from that message. Checked against it before the verdict is applied.
    quote: str | None = None
    reason: str | None = None


class RecheckResponse(Strict):
    verdicts: list[RecheckVerdict] = Field(default_factory=list)


class RelevanceVerdict(Strict):
    """One open obligation, judged against what the ledger records about the owner.

    `cites_fact` is the whole difference between this pass and a guess. A `nonsense`
    verdict names the recorded fact that retired the obligation — the enrolment that
    makes another university's deposit moot — and that id is intersected with the fact
    table before anything is dropped, the same guard `cites` gets in `RecheckVerdict`.

    `quote` is copied from the obligation's own source text and checked against it, so a
    fluent-sounding paraphrase cannot stand in for having read the thing being retired.
    """

    commitment_id: int
    verdict: Literal["nonsense", "keep"]
    confidence: float = Field(ge=0.0, le=1.0)
    #: `fact.id` of the recorded fact that makes it moot. None is only valid for `keep`.
    cites_fact: int | None = None
    #: Verbatim, from the obligation's source item. Checked before the verdict is applied.
    quote: str | None = None
    reason: str | None = None
    #: What this obligation's standing rests on: `fact.id`s that would have to stay true
    #: for a `keep` to remain a keep. Required on EVERY verdict, including keeps — that is
    #: the whole point of asking (v3, 2026-08-24). `logic_check` is judged once per
    #: commitment, so a `keep` issued when the facts said one thing was never revisited
    #: when the facts said another; the checker had no way to notice the situation moved.
    #: These ids are the invalidation index that gives it one: fact superseded, look up
    #: its dependents, re-judge only those.
    #:
    #: An empty list is a first-class answer and means "depends on no recorded fact" —
    #: "zip it up once done" rests on nothing, and forcing a citation onto it would invent
    #: exactly the evidence the citation rule exists to prevent.
    depends_on: list[int] = Field(default_factory=list)


class RelevanceResponse(Strict):
    verdicts: list[RelevanceVerdict] = Field(default_factory=list)


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
