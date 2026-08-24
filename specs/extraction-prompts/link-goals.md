---
id: link-goals
version: 1
model: careful
output: strict JSON
---

# Which long-term goal, if any, does this obligation serve

The owner keeps eight goals with roadmaps under them — 28 dated steps and 61 targets. On
2026-08-23, `goals.health` said five of the eight were at risk, and **one open commitment
out of 387 carried a `goal_id`**. So the planner has a whole priority tier for "advances a
goal that is at risk" and exactly one row it could ever promote into it. The daily work and
the long-term plan are two records of the same life that have never been introduced.

Nothing is wrong with either half. `checkpoints.link_commitment` is the only writer of that
column and it has two callers, both of which are somebody typing a command. The link has
simply never been made at the scale the ledger grew to.

**A text matcher was tried first and is why this pass exists.** Matching commitment text
against goal, target and step titles linked 78 of 387 — and 72 of those came from the word
`submit`, which belongs to goal 8 only because that goal's title happens to contain it.
"Submit MMR immunization records" is not an external scholarship. Strip the generic verbs
and the honest yield was about six rows. The signal that decides this is meaning, not
vocabulary, which is what a model is for and a regex is not.

## Prompt

```
You are deciding which of the user's long-term goals each of their open obligations
moves forward, if any.

You will be given, in this order: the user's goals, each with an id and the targets
that define what progress on it looks like; then open obligations, each with an id,
who it came from, and what it says.

For EACH obligation return one answer: the id of the goal it moves forward, or null.

null is the ordinary answer and carries no penalty. Most of what anyone owes in a
given week belongs to no long-term goal at all, and a life is not a project plan.

Rules:

1. A link MUST carry `quote`: words copied exactly from the obligation's own text
   that show what it is. Not from the goal, not a paraphrase, not your reasoning.
   A link whose quote is not found in the obligation is discarded and never
   applied — the quote is what makes the link checkable a year from now.

2. One goal, or none. If two goals both look plausible, answer null. Guessing
   between them is worse than leaving the obligation where it already sits.

3. Read each goal's targets, not only its title. "Get into a competitive med
   school" contains none of the words shadowing, MCAT or clinical hours, and its
   targets do. An obligation to call hospices about volunteering reaches that goal
   through a volunteering-hours target, and the title alone would never show it.

4. A named project is strong evidence. The user's product goals carry distinctive
   names, and an obligation that names one is almost certainly that goal's work.
   An obligation that merely reuses a common word from a goal's title is not —
   "Submit MMR immunization records" is not an external scholarship.

5. Coursework is not automatically a goal. An assignment is schoolwork; it links
   only if a target names that course or programme. Most coursework is null.

6. Do not judge whether the obligation is worth doing. Something overdue, or
   something you would not have chosen, still links if it serves the goal.
   Retiring obligations is another pass's job with its own evidence rules.

7. When unsure, null with lower confidence. A wrong link puts a row higher in
   tomorrow's plan, where the user sees it and undoes it in one click; a missing
   link changes nothing at all. Neither deletes anything, so do not reach.

`confidence` is your certainty in the answer, 0.0 to 1.0.

The user's goals, with the targets that define what progress on each looks like:
{{goals}}

Obligations to judge:
{{commitments}}
```

## Fixtures

`tests/fixtures/goal_linking/` — the negatives carry the weight, as in `check-relevance`.

| fixture | shape | expected |
|---|---|---|
| `01-named-project.json` | "OrgTruth: run e2e:live against a real key" | links to the OrgTruth goal — rule 4 |
| `02-target-not-title.json` | "Call the phone-only hospices about volunteering" | links to med school via a volunteering target — rule 3 |
| `03-generic-verb.json` | "Submit MMR immunization records" | `null` — the exact false positive the matcher produced |
| `04-coursework.json` | "Complete CIS236 assignment 1-1-1" | `null` — rule 5 |
| `05-unquoted.json` | a link whose quote is not in the obligation | discarded in code, never applied |
| `06-two-goals.json` | plausibly two goals | `null` — rule 2 |
