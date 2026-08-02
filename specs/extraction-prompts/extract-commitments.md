---
id: extract-commitments
version: 3
model: careful
output: strict JSON, schema-validated, one retry on malformed
---

<!-- v2 (2026-07-30): the live eval showed the model returning zero commitments for a
     resolving message — "do not extract completed things" appeared before the resolves
     rule and won. Resolutions now come first, with an explicit carve-out in the
     do-not-extract list. Eval finding #1 (the model dedupes thread restatements itself)
     is welcome; the code-level dedup in post-processing stays as the backstop. -->

<!-- v3 (2026-08-01): the prompt's date rule stated the principle (resolve against the
     message date) but not the conventions, so the model and extract/dates.py could
     disagree on "Friday", "end of week", and "next week". The resolver's conventions
     are now spelled out in the prompt — one source of truth, stated twice, identical.
     Also: counterparty direction made explicit; the eval showed occasional swaps on
     owed_to_me. -->

# Tier 2 commitment extraction

Runs only on items where triage returned `keep=true`. This is where money is spent and it
is worth spending.

## Prompt

```
Extract commitments from this message.

A commitment is a specific obligation with an owner. Two directions:
  i_owe        the user promised to do or provide something
  owed_to_me   someone promised the user something

The user is {{owner_name}} <{{owner_email}}>.

For each commitment, return:
  direction          i_owe | owed_to_me
  counterparty       the other party's name or email as written — for i_owe,
                     who the user owes; for owed_to_me, who owes the user
  what               the artifact or action, in under 12 words, concrete
  due_at             ISO 8601 date or datetime, or null if none stated
  due_is_explicit    true if a date was stated, false if you inferred it
  estimated_minutes  integer, only if the message supports one; else null
  confidence         0.0 to 1.0
  evidence           the exact sentence you extracted it from, verbatim

DATE RESOLUTION — this is the most important rule here.
Resolve all relative dates against the message date {{occurred_at}}, never
against today. "By Friday" in a message sent 2026-07-10 means 2026-07-17,
even if today is 2026-08-30. Getting this wrong produces confidently wrong
briefs, which is the worst outcome this system has.

Conventions (these match the ledger's own resolver — do not improvise):
  - A bare weekday ("Friday", "this Friday") is the first such day strictly
    after the message date, never the message's own day.
  - "Next <weekday>" skips a week only when the nearer one is under a week out.
  - "End of week" is that week's Friday; "next week" adds seven days;
    "end of month" is the last day of the message's month.
  - Return a date alone unless the message states a clock time. Never invent
    times and never convert timezones — keep what was written.

CONFIDENCE
  0.9+   explicit commitment, explicit date, unambiguous owner
  0.7    explicit commitment, inferred date or ambiguous owner
  0.5    hedged language ("should be able to", "will try")
  <0.5   you are guessing — return it anyway, it goes to a review queue

RESOLUTIONS — check this before deciding something is "already done".
If the message delivers or completes an earlier commitment ("here's that plan
I promised", "sent the deck last night"), you MUST still return one entry for
it: set resolves=true, describe the earlier commitment in resolves_what, and
set what to the thing delivered. A resolution is not a skip — it is how the
ledger closes the original promise.

Do not extract:
  - aspirations with no counterparty ("we should really fix that")
  - things already completed, UNLESS they resolve an earlier commitment —
    then return them with resolves=true as above
  - commitments between two other people that do not involve the user
  - restatements of a commitment already made in an earlier quoted message

MESSAGE
From: {{author}}
To: {{recipients}}
Date: {{occurred_at}}
Subject: {{title}}

{{body_text}}
```

## Output schema

```json
{
  "commitments": [
    {
      "direction": "i_owe",
      "counterparty": "Dana Whitfield <dwhitfield@example.gov>",
      "what": "revised migration plan",
      "due_at": "2026-07-17",
      "due_is_explicit": true,
      "estimated_minutes": null,
      "confidence": 0.92,
      "evidence": "I'll have the revised migration plan over to you by Friday.",
      "resolves": false,
      "resolves_what": null
    }
  ]
}
```

Empty `commitments` array is a valid and common result. Do not invent one to be useful.

## Post-processing, in code not prompt

1. Resolve `counterparty` to an `entity` via the alias table; create on miss.
2. Apply the type default for `estimated_minutes` when null. Record
   `estimate_source`.
3. If `confidence` is below the configured threshold (default 0.7), the commitment is
   created with status `open` but is excluded from the brief and appears in the review
   queue.
4. If `resolves` is true, find the matching open commitment and set it `superseded` with
   `superseded_by` pointing at the new row. Never delete.
5. Deduplicate against existing open commitments on `(direction, counterparty, what)`
   fuzzy match before insert. A thread restating the same promise must not produce five
   rows.

## Failure handling

- Malformed JSON: retry once with the schema restated. On second failure, park the item
  with `extraction_version` unset so it is retried on the next prompt version.
- Never partially apply a malformed response.
- An item that fails twice is surfaced in the dashboard, not silently skipped.

## Fixtures

Every change to this prompt requires the fixture set in `tests/fixtures/commitments/` to
pass. The fixtures must include, at minimum:

- an explicit commitment with an explicit date
- a relative date in a message that is several weeks old
- a hedged commitment that should land near 0.5
- a thread where the same commitment is quoted three times
- a message that resolves an earlier commitment
- a message with commitments between two third parties, which must extract nothing
- a message in which the owner is in Cc rather than To
