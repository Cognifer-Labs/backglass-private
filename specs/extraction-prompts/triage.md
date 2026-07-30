---
id: triage
version: 1
model: small/fast
output: strict JSON
---

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

When genuinely uncertain, return keep=true. A false positive costs one extraction
call. A false negative loses a commitment permanently, and the user will never
know it happened.

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
