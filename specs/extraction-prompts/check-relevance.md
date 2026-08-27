---
id: check-relevance
version: 3
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

All three placeholders sit at the very end of the block below, which is a cost decision
rather than a formatting one: `Prompt.split` caches everything before the first
placeholder, and the rules are identical on every call while the facts, the situation and
the batch are not.

## Version 2: the situation, not only the facts (2026-08-24)

Version 1 handed the judge a list of atomic facts and nothing else. That is enough to see
that a UT Dallas deposit contradicts an ASU enrolment, and it is not enough to see what
*recently* changed — which is the event that moots things — or what the week actually
holds. `backglass/situation.py` renders the state doc; this pass carries it minus its
facts section, because the facts already appear above under the header a citation is
validated against, and two copies of the same claims would leave the model choosing which
list to cite from.

Nothing about the citation rule moves. A `nonsense` verdict still names a fact id from the
list under "What the ledger records about the user", and the code still intersects that id
with the active set before anything is dropped. The situation block is context for the
judgement, never a source of ids: no line in it may be cited, because a line rendered from
the board is not a claim with provenance.

The bump costs nothing in re-judgment: `logic_check` is keyed on `commitment_id` alone
with no prompt version in it, so already-judged obligations stay judged and only rows that
were never sent are sent. That is deliberate — a version bump here must never re-open two
hundred settled verdicts and re-pay for them.

## Version 3: what each obligation rests on (2026-08-24)

Versions 1 and 2 asked one question — has a recorded fact made this moot? — and the answer
was stored once per commitment, forever. That is the owner's complaint restated in schema:
*"the checker has no way to notice the situation moved."* A `keep` issued while the ledger
said one thing was never revisited when it said another.

So every verdict now also answers **what it rests on**: the fact ids that would have to
stay true for a `keep` to remain a keep. Not the citation — that is `cites_fact`, and it
still only appears on a `nonsense` — but the dependency, which is the thing that lets the
system come back later. When one of those facts is superseded or retracted, its dependents
are looked up and re-judged, and nothing else is.

Two properties this must have, and the prompt says both below:

- **`depends_on` is required on every verdict, keeps included.** A dependency recorded only
  at drop time is useless: the row is already closed. The whole value is in the keeps.
- **An empty list is a real answer.** "Zip it up once done" rests on no recorded fact about
  the user, and forcing a citation onto it would invent exactly the evidence the citation
  rule exists to prevent. Say `[]` and mean it.

The ids are intersected with the facts actually sent, like every other id crossing this
boundary. A bad id in `depends_on` is dropped rather than voiding the verdict — a citation
justifies closing something and must be right or absent, while a dependency is a note about
what to re-check, and a narrower index is not a wrong answer.

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

8. EVERY verdict must also give `depends_on`: the ids of the facts this obligation's
   standing rests on — the ones that, if they changed, would make you want to look at
   this obligation again. A `keep` that rests on "the user is enrolled at ASU" says
   so; if enrolment changes, that keep gets re-examined and nothing else does.

   Give `[]` when the obligation rests on no recorded fact. That is a real answer and
   the right one for personal and household obligations: "bring the bedsheet to wash"
   depends on nothing in the list. Do not reach for a loosely related fact to fill it.

   `depends_on` is not a citation. `cites_fact` justifies retiring something and belongs
   only to `nonsense`; `depends_on` is what to re-check later and belongs to every verdict.

What the ledger records about the user (cite these ids, and only these):
{{facts}}

The user's situation right now — what recently changed, what is open this week, and
what other obligations already rest on. Context for your judgement; it carries no ids
you may cite:
{{situation}}

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
