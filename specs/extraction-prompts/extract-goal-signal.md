---
id: extract-goal-signal
version: 3
model: careful
output: strict JSON
---

<!-- v2 (2026-08-01): delta was documented as "usually 1", which was written before
     hour-denominated totals existed (Phase 10 medical preset: shadowing/clinical/
     research hours). A message evidencing "four hours in the ER" must move an hour
     target by 4, not 1 — and a stated amount is the only acceptable source for a
     delta above 1. Units rule added. -->


# Goal signal extraction

Runs on kept items, after commitment extraction, only when the user has active goals.
Its job is narrow: decide whether this item is evidence that a goal moved.

It does **not** create goals. Goals are entered by hand, always. Inferred goals are
uniformly wrong, and the user is choosing perhaps eight things a year, so typing them is
not the bottleneck.

## Prompt

```
Decide whether this message is evidence that work happened on any of the user's
active goals, which are listed below the rules.

Evidence means the work actually occurred or shipped. It is not:
  - a plan to do the work later (that is a commitment, already captured)
  - a discussion about the goal
  - someone else asking about it

Return at most one target per goal. If nothing applies, return an empty array.
That is the common case and the correct answer most of the time.

For each signal:
  target_id    which target advanced
  delta        units moved, in the target's own unit — hours for hour targets,
               a count (usually 1) otherwise. Use only amounts the message
               states; if work clearly happened but no amount is stated,
               return delta 1 with confidence at or below 0.4 so it routes
               to review instead of the ledger
  confidence   0.0 to 1.0
  evidence     the exact sentence, verbatim

ACTIVE GOALS
{{#goals}}
  [{{id}}] {{title}} — done when: {{definition_of_done}}
     targets: {{#targets}}({{id}}) {{title}}{{/targets}}
{{/goals}}

MESSAGE
Date: {{occurred_at}}
From: {{author}}
Subject: {{title}}

{{body_text}}
```

## Output schema

```json
{
  "signals": [
    {
      "target_id": 4,
      "delta": 1,
      "confidence": 0.81,
      "evidence": "Migration plan is out to DOH as of this morning."
    }
  ]
}
```

## Post-processing

1. Below the confidence threshold, the signal goes to the review queue rather than
   creating a checkpoint.
2. Above threshold, create a `checkpoint` with `source='extraction'` and the
   `source_item_id` set. Provenance is required.
3. One checkpoint per `(target_id, source_item_id)`. Re-extraction must not double-count,
   and this is enforced by a unique constraint rather than by trusting the model.

## Why this is a separate prompt

Folding goal signal into commitment extraction was tried in design and rejected. The two
tasks have different precision requirements: a missed commitment is a real loss, while a
missed goal checkpoint is a rounding error on a weekly count. Combining them pushes the
combined prompt toward the looser standard.

Running it separately also means goals can be added later without re-extracting
commitments.
