---
id: triage-batch
version: 2
model: small/fast
output: strict JSON
---

<!-- v2 (2026-08-01): mirrors triage.md v2 — keep-tests-outrank-drop-list precedence
     and the confirmation-reply rule. The two prompts ask the same question and must
     change together. -->


# Tier 1 triage, batched

The same binary question as triage.md, asked over many messages in one call so the
instruction tokens are paid once instead of once per item. Excerpts are short (500
characters); anything the model is not confident about on an excerpt escalates to the
full per-item pass — precision is never traded for the batching discount.

## Prompt

```
You are triaging a batch of independent messages. For each one, decide whether it is
worth a careful second pass.

For each message, return keep=true only if it plausibly contains at least one of:
  - a commitment someone made (to do something, send something, decide something)
  - a deadline or date by which something must happen
  - a decision that was reached
  - a fact about an ongoing project that would matter in a week

Return keep=false for: newsletters, marketing, receipts, shipping notices,
automated alerts, social notifications, mailing list chatter, and pleasantries
with no substance.

The keep tests outrank the drop list: a receipt or automated alert that states
a deadline the user must act on (payment due, document expiring, submission
window closing) is a keep. A short reply that accepts or confirms something
("yes, Friday works") is a decision reached — keep it.

Each excerpt is truncated to 500 characters. If an excerpt is not enough to be
confident the message contains none of the above, set uncertain=true — it will be
re-read in full. When genuinely uncertain between keep and drop, set keep=true.

Return exactly one entry per message id: no more, no fewer, and only ids that
appear below. `reason` must quote or paraphrase the specific trigger.

MESSAGES
{{items}}
```

## Output schema

```json
{
  "items": [
    {"id": 123, "keep": false, "reason": "shipping notice", "uncertain": false},
    {"id": 124, "keep": true, "reason": "'I will send the draft Friday'", "uncertain": false}
  ]
}
```

## Notes

- Each item renders as `--- id: <id>` then From/Date/Subject and the 500-char excerpt.
- The keep-when-uncertain asymmetry is triage.md's and is not tunable for cost here
  either: uncertainty escalates to the full 2000-char per-item pass, whose verdict is
  the one recorded. Nothing is ever dropped on an excerpt the model hedged about.
- Ids the model returns that were not sent are ignored. Ids sent but missing from the
  response, returned twice with disagreement, or marked uncertain are escalated. A
  batch that fails validation escalates every item in it (degrade, never block).
