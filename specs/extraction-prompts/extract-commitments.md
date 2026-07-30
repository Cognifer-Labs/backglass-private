---
id: extract-commitments
version: 1
model: careful
output: strict JSON, schema-validated, one retry on malformed
---

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
  counterparty       the other party's name or email as written
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

CONFIDENCE
  0.9+   explicit commitment, explicit date, unambiguous owner
  0.7    explicit commitment, inferred date or ambiguous owner
  0.5    hedged language ("should be able to", "will try")
  <0.5   you are guessing — return it anyway, it goes to a review queue

Do not extract:
  - aspirations with no counterparty ("we should really fix that")
  - things already completed and reported in past tense
  - commitments between two other people that do not involve the user
  - restatements of a commitment already made in an earlier quoted message

If the message resolves an earlier commitment ("here's that plan I promised"),
set resolves=true and describe the commitment it resolves in resolves_what.

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
