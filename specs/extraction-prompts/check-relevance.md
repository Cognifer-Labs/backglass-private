---
id: check-relevance
version: 1
model: careful
output: strict JSON
---

# Obligations the owner's own record has overtaken

The board on 2026-08-18 gave four of a twelve-block day to a UT Dallas scholarship
acceptance whose deadline passed on 1 May, alongside a Baylor enrolment deposit, a
Northern Arizona enrolment fee and a University of Arizona Next Steps payment. The owner's
verdict was one sentence: *"the AES things on schedule make no sense because i go to
asu"*, and the ledger already knew — `fact` row 5 records ASU Tempe, Barrett Honors,
incoming fall 2026.

Nothing consumed that. The extraction was correct at the time (each mail really did ask
for the thing), the staleness gate stopped scheduling them, and the queue would have asked
about eighty-three of them five at a time. None of that is the same as knowing that
choosing one university retires every other university's admissions paperwork at once.

This pass is that knowledge. It is handed what the ledger records about the owner and a
batch of open obligations, and asked one question each: **has something the owner has
already recorded made this obligation moot?**

## The rule that matters most

**A `nonsense` verdict must name the fact it contradicts.** Not a feeling that the item
looks old, not "this seems irrelevant" — the id of a specific recorded fact, plus the
words from the obligation's own source that identify what is being retired. A verdict
that cannot cite both is discarded rather than repaired, exactly as in
`recheck-commitments`.

This is the difference between the pass the owner asked for and silent data loss. "Nobody
mentioned it since" is not this pass's business — that is `staleness`, and it asks rather
than acts. What this pass acts on is a *positive contradiction*: the owner enrolled here,
so the other one's deposit is not owed; the owner declined this program, so its forms are
not owed.

## Asymmetry, on purpose

Above the drop threshold the obligation is dropped, tombstoned, with the citation in its
`resolution_note`. Below it, nothing is dropped and the owner gets one question. A wrong
`keep` is a row the owner sees and can dismiss in a click. A wrong `nonsense` is a
disappearance, and four entries in tasks/lessons.md are about paying for one of those.

Both placeholders sit at the very end of the block below, which is a cost decision rather
than a formatting one: `Prompt.split` caches everything before the first placeholder, and
the rules are identical on every call while the facts and the batch are not.

## Prompt

```
You are checking whether the user's open obligations still make sense, given what
is recorded about the user's life.

You will be given, in this order: what the ledger records about the user, each line
with a fact id; then open obligations, each with an id, the words it was extracted
from, and who it came from.

For EACH obligation return exactly one verdict:

  nonsense  a recorded fact has made this obligation moot — the decision it belongs
            to has already been made the other way, the programme was declined, the
            institution is not the one the user is part of
  keep      anything else

Rules:

1. A `nonsense` verdict MUST name `cites_fact`, the id of the fact that makes it
   moot, and `quote`, words copied verbatim from that obligation's source text. No
   citation, no verdict — return `keep` instead.

2. Being overdue is NOT nonsense. A missed deadline is usually a real obligation the
   user failed to meet, and this pass must never be the reason one disappears. Only
   a fact that makes it moot counts.

3. Being old, small, vague or badly worded is NOT nonsense. Extraction quality is
   somebody else's problem.

4. One decision can retire many obligations. If the user enrolled at one university,
   every other university's deposit, housing form, scholarship acceptance and
   waitlist reply is moot — return `nonsense` for each, citing the same fact.

5. An obligation to a place the user IS part of is never moot on those grounds.
   Read the sender and the words: the same "submit the housing form" is live from
   the user's own institution and moot from one they turned down.

6. Personal, family and social obligations are almost never mooted by a fact.
   Prefer `keep` unless the fact plainly ends them.

7. When unsure, `keep` with lower confidence. The user sees a kept row and clicks
   once; a wrongly dropped row disappears silently.

confidence is your certainty in the verdict, 0.0 to 1.0.

What the ledger records about the user:
{{facts}}

Open obligations to judge:
{{commitments}}
```

## Fixtures

`tests/fixtures/relevance/` — the negatives carry the weight, as in `recheck-commitments`.

| fixture | shape | expected |
|---|---|---|
| `01-other-university.json` | UT Dallas scholarship acceptance, fact says enrolled ASU | `nonsense`, cites the enrolment fact |
| `02-own-university.json` | ASU housing form, same fact | `keep` — rule 5 |
| `03-merely-overdue.json` | a real obligation months past due, no fact touches it | `keep` — rule 2 |
| `04-uncited.json` | `nonsense` with no fact id | discarded in code, never applied |
| `05-family.json` | "bring the bedsheet to wash" | `keep` — rule 6 |
