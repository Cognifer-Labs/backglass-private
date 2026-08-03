---
id: extract-commitments
version: 5
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

<!-- v4 (2026-08-02): the read now also returns engagements — plans to be somewhere with
     someone. They were invisible before: not a commitment (nobody owes anything), and
     calendar invites are dropped at tier 0 as "owned by the calendar connector", which
     reads nothing on an account with no calendar connected. Same call, not a second
     pass: a separate careful read to ask a second question would double extraction cost
     per item for the life of the system. -->

# Tier 2 commitment extraction

Runs only on items where triage returned `keep=true`. This is where money is spent and it
is worth spending.

## Prompt

```
Extract two kinds of record from this message: commitments and engagements.

A commitment is a specific obligation with an owner. Two directions:
  i_owe        the user promised to do or provide something
  owed_to_me   someone promised the user something

An engagement is a plan to be somewhere with someone — dinner with a friend,
a conference, an interview, office hours, a call. Nobody owes anybody an
artifact; the substance is the meeting itself.

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

For each engagement, return:
  kind               social | professional
  what               the occasion, in under 12 words ("dinner at Ravi's",
                     "AAMC advising call", "BioBridge volunteer shift")
  people             everyone going other than the user, names or emails as
                     written; [] if the message names nobody
  starts_at          ISO 8601 date or datetime, or null if no time is agreed
  ends_at            ISO 8601, or null — only if the message states an end
  when_is_explicit   true if a date was stated, false if you inferred it
  location           as written, or null
  status             proposed | confirmed | declined
  replaces_earlier   true if this MOVES a plan that was already arranged
  confidence         0.0 to 1.0
  evidence           the exact sentence you extracted it from, verbatim

SOCIAL OR PROFESSIONAL
  social         friends, family, anything whose purpose is the company
  professional   work, study, medicine, research, admissions, networking —
                 anything you would put on a CV or prepare for
When a plan is plainly both (a mentor who is also a friend), choose by why it
is happening, not by who is going.

STATUS
  proposed   someone suggested it and nobody has agreed yet, including when
             the user is the one who suggested it
  confirmed  both sides have agreed, or it is stated as settled fact
  declined   it was turned down or cancelled
"Dinner Friday?" is proposed. "Friday works, see you at 7" is confirmed.
An invitation the user has not answered stays proposed — that is the whole
point of tracking it, because it is what the user still owes a reply to.

RESCHEDULING — set replaces_earlier when a message moves an existing plan.
"Can we push dinner to 7:30", "let's do Saturday instead", "moving lunch to
1pm" are all the SAME plan at a new time: return one engagement, with the
NEW time, and replaces_earlier=true.
Leave it false when the message proposes something additional, even on the
same day and with the same people — "coffee at 9, or 4 if that's easier" is
two options, and "lunch Friday and drinks Friday" is two plans. If you are
unsure, leave it false: a plan that turns out to be a duplicate is visible
and can be dismissed, whereas a wrongly merged one silently replaces a plan
the user already agreed to.

COMMITMENT OR ENGAGEMENT — do not return the same thing as both.
Ask what the message is actually about. If it is about producing or sending
something, it is a commitment, even when a meeting is mentioned as the
deadline ("I'll have the slides ready before we meet Tuesday" is a
commitment). If it is about being somewhere with someone, it is an
engagement, even when the user promised to attend ("I'll be at your defence
Tuesday" is an engagement). A message can legitimately produce one of each
when it contains both ("I'll send the draft Thursday, and are you free for
lunch Friday?") — that is two records about two different things, not a
double count.

Do not return an engagement for:
  - a meeting between other people that does not involve the user
  - a mass invitation with no personal element (a newsletter's webinar, a
    building-wide fire drill, a marketing event blast)
  - something already over, unless the message is arranging the next one

DATE RESOLUTION — this is the most important rule here.
It applies to `due_at` and to `starts_at`/`ends_at` alike.
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
  ],
  "engagements": [
    {
      "kind": "social",
      "what": "dinner at Ravi's",
      "people": ["Priya Raman <priya@example.com>", "Sam"],
      "starts_at": "2026-07-17T19:00",
      "ends_at": null,
      "when_is_explicit": true,
      "location": "Ravi's on 5th",
      "status": "confirmed",
      "replaces_earlier": false,
      "confidence": 0.88,
      "evidence": "Friday works — 7pm at Ravi's on 5th, Sam's coming too."
    }
  ]
}
```

Both arrays are independent, and an empty one is a valid and common result. Most
messages produce neither. Do not invent either to be useful.

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

Engagements take the same five steps, with two differences that follow from what they
are (`backglass/extract/engagements.py`):

6. Every name in `people` resolves to its own `entity`; the links live in
   `engagement_person`, so one plan can involve several people.
7. Dedup matches on `(what, people, day)` rather than on direction. A match does not
   drop the sighting — it applies the new `status` to the row it matched, which is how
   "dinner Friday?" becomes confirmed when the reply arrives, and records the sentence
   as a `restated` citation. A status only ever moves forward: proposed → confirmed →
   done, or → declined. Nothing walks back to proposed, because a later message
   restating an agreed plan is not a fresh proposal.

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
