---
id: triage
version: 3
model: small/fast
output: strict JSON
---

<!-- v3 (2026-08-07): the prompt learns who it is triaging for. `{{owner_context}}`
     carries the `fact` table — the owner's own recorded facts — so the model can tell
     their obligations from broadcast marketing. It could not before: a university's
     admissions mail is a deadline to a prospective student and noise to one who has
     already enrolled elsewhere, and nothing in this prompt could tell those apart.
     The block is STATED, never instruction: it says who the owner is, it does not say
     what to drop, and the keep-bias below is untouched. It renders empty on a ledger
     with no facts, so a fresh install sends the v2 prompt byte for byte. Placed after
     the instructions so Prompt.split() still hands a caching backend a static prefix.
     Mirrored in triage-batch.md, per the note below. -->
<!-- v2 (2026-08-01): two failure modes closed. (1) The keep tests and the drop list
     could conflict — a receipt carrying a payment deadline matched both, and the model
     picked either. Precedence is now explicit: keep tests outrank the drop list.
     (2) Short confirmation replies ("yes, Friday works") read as pleasantries and were
     dropped, losing the decision they carry. Any change here must be mirrored in
     triage-batch.md — the two prompts ask the same question. -->


# Tier 1 triage

Runs only on items the rule layer could not classify. Rules handle bulk headers,
`no-reply` senders, known-noise domains, and calendar invites before this prompt is
reached.

One binary question, nothing else. Every token spent here multiplies across the whole
inbox.

## Prompt

```
You are triaging one message to decide whether it is worth a careful second pass.

Return keep=true only if the message plausibly contains at least one of:
  - a commitment someone made (to do something, send something, decide something)
  - a deadline or date by which something must happen
  - a decision that was reached
  - a fact about an ongoing project that would matter in a week

Return keep=false for: newsletters, marketing, receipts, shipping notices,
automated alerts, social notifications, mailing list chatter, and pleasantries
with no substance.

The keep tests outrank the drop list: a receipt or automated alert that states
a deadline the user must act on (payment due, document expiring, submission
window closing) is a keep.

A short reply that accepts or confirms something ("yes, Friday works",
"approved, go ahead") is a decision reached — keep it even though it looks
like a pleasantry.

When genuinely uncertain, return keep=true. A false positive costs one extraction
call. A false negative loses a commitment permanently, and the user will never
know it happened.

{{owner_context}}

MESSAGE
From: {{author}}
Date: {{occurred_at}}
Subject: {{title}}

{{body_text_truncated_2000}}
```

## Output schema

```json
{
  "keep": true,
  "reason": "commitment language: 'I'll send the revised scope by Thursday'"
}
```

`reason` is required and must quote or paraphrase the specific trigger. It is what makes
triage tunable: reading a week of reasons is how you find the rule that should have caught
something before it reached the model.

## Notes

- Truncate the body to 2000 characters. Quoted thread history is stripped upstream.
- Record the verdict and reason on `source_item`. Never discard the reason.
- Target kill rate is 90–95 percent. Below 85 percent, the rule layer has drifted and
  cost is about to climb. Surface this in the Sources panel.
- The asymmetry in the uncertainty instruction is deliberate and should not be tuned away
  to save money. Cap spend instead.
