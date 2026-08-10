# Day planner — what it still needs

> **Status, 2026-08-09 evening.** Written before a concurrent session landed six commits
> on `main` covering most of Tier 1. Re-measured after merging them; the state of each
> item is marked below. Five items are now done, two were superseded by that session's
> work, and the remainder are real and unstarted.
>
> | Item | State |
> |---|---|
> | 1. An hour in prose | **superseded** — prompt v8 makes "be somewhere at 8am" an engagement (`2af4508`). Existing rows still predate it; see §12. |
> | 2. Overdue outranks due-today | **superseded in practice** — with hours on engagements, a day-anchored event is a fixed block and never competes for ranking. Left open as a spec gap in docs/04 §1.5. |
> | 3. Shutdown at 18:00 | **done** — `141eba6` |
> | 4. Duplicates never merge | **done, differently** — `35f7414`. The merge would have been wrong; see the revised item. |
> | 5. Half the ledger invisible | open |
> | 6. Goal linkage dead | open — the largest remaining item |
> | 7. Estimates are guesses | **unblocked by 3** — starved of input, not broken; no code |
> | 8. Routines have no weekday | **done** — `58c5f93` |
> | 9. `state` misses templates | **done** — `078b301` |
> | 10. "46 did not fit" | **done** — `34b48dd` |
> | 11. Lane readability | **done** — `dbb1f8f` |
> | 12. Re-extract the old rows | **new**, and now the top of the list |

Derived 2026-08-09 by measuring the live store, not by reading the spec and guessing.
Every claim below names the command behind it, so it can be re-derived rather than
believed. Written to its own file rather than `tasks/todo.md` because a second session
was editing that file at the time.

**What is already done, so nobody re-opens it.** docs/04's P1–P16 are implemented and
covered by 61 tests in `tests/test_planner.py` — capacity arithmetic, the never-zero
reserve, deep-work protection, rollover, the drop-or-do question, and three timezone
cases including Phoenix→Coimbatore. `plan/estimates.py` has the estimated-vs-actual
machinery of §1.3. `goals/checklist.py` has C1–C6. `dedup.py` finds duplicate
commitments and the dashboard already asks about them. The gap is not the algorithm.
It is what the algorithm is fed, plus the three defects in Tier 1.

---

## Tier 1 — today's plan is wrong, and the code is why

### 1. An hour stated in prose never becomes an hour on the calendar

Move-in was due 2026-08-09, confidence 1.0, and spent the day in the did-not-fit list.
The 8:00am is written in the commitment's title and nowhere a planner can read it.
Six `engagement` rows describe this one event; five have `starts_at = NULL` and the
sixth carries 2026-08-05, the Early Start date the owner declined.

```sql
SELECT id, what, starts_at, status FROM engagement WHERE what LIKE '%move-in%';
```

`a_plan_with_a_day_but_no_hour_is_not_placed` is deliberate and correct — the bug is
upstream, in never extracting the hour that was stated.

- **Implement**: extraction fills `engagement.starts_at` when the source text names a
  wall-clock time; ledger-level engagement dedup so one event is one row.
- **Test**: an engagement whose text says "8:00am" becomes a placed fixed block, and
  six restatements of one event collapse to one.

### 2. Overdue outranks due-today unconditionally

`planner.candidates` assigns `PRIORITY_OVERDUE` above `PRIORITY_DUE_TODAY`, with no
notion of an item anchored to a day. So a loan form due 2026-08-07 took the protected
peak block on the morning the owner moved into their dorm, and the move-in fell off
the day entirely. Both rows are `rolled=1, conf=1.0, 30m`, so nothing else separated
them.

docs/04 §1.5 does not rank day-anchored events, which is the actual omission.

- **Implement**: an item that names a wall-clock hour and is due today is anchored, not
  ranked — it is placed before selection begins, like a fixed event.
- **Test**: `a_day_anchored_item_is_never_dropped_for_an_overdue_one`.

### 3. The evening pass fires four hours before the day ends

`com.backglass.shutdown.plist` runs at 18:00. `WORKING_WINDOW` is `10:00-22:00`, and
the owner is at the gym at 17:30. Shutdown asks what got done while a third of the
working day is still ahead of it.

- **Implement**: render the shutdown hour from the working window rather than hardcoding
  it in the plist template.
- **Test**: the installed shutdown hour equals the working window's end.

---

## Tier 2 — the ledger the planner reads

### 4. Cross-source duplicates never merge

70 suspect pairs are open right now, seven of them byte-identical:

```
3x  Send instructor intro email from ASU address
2x  complete Dreamscape waiver online
2x  give a ride
```

`dedup_threshold` is 0.85 and pairs scoring 1.0 are still open. The mechanism is the
counterparty, not the score: `extract/commitments.py:260` merges against
`ledger.open_commitments_for(direction, entity_id)`, so two rows are only ever compared
when both resolved to the same person. The three identical rows above carry entities
155, 156 and 157; the Dreamscape pair carries 80 and 81; "give a ride" carries 30 and
NULL. Nothing ever compares them.

`dedup.py`'s own docstring already names this as the reason its question-queue matches
on ANY counterparty — the automatic path never got the same treatment. Semantically:
8 open scholarship rows, 8 hospice, 6 diploma.

**The merge this item originally proposed would have destroyed data, and was not built.**
Those three identical rows carry entities 155, 156 and 157 — Suriyampola, Hossain and
Pedram — and all three came from source item 8763. One mail asking for three intro
emails is three promises, and an entity-blind auto-merge at ≥0.95 would have silently
deleted two of them. Nineteen of the pairs are this shape.

What shipped instead (`35f7414`): the question carries the fact that answers it. Each
side names its counterparty, and a pair drawn from one message to two different people
says so. Every top pair in the owner's store is now decidable by eye — three instructors
are Different; "Complete the Math Placement Test — ASU" against the same words with no
counterparty is Same. Labelled, never merged, because same-source-different-person also
covers one task read twice with the sender resolved differently each time, and this layer
cannot tell those apart.

- **Still open**: "complete Dreamscape waiver online" is owed to both "Nyasha" and
  "Mrs. Shepard" — one person under two names. Entity merging is the deeper fix and its
  own job.

### 5. Over half the owner's obligations are invisible to the planner

```
132 open → 108 i_owe → 48 above the 0.7 confidence floor
```

176 extractions await review. Rule 2 is right — a guess must not enter the brief as
fact — so the fix is throughput, not a lower threshold.

- **Implement**: bulk accept/reject in the review queue, ordered by how much a decision
  would change the next plan.

### 6. Goal linkage is dead code against real data

One of 132 open commitments has a `goal_id`, and the med school goal — the one the whole
roadmap hangs off — has none. `PRIORITY_AT_RISK_GOAL` sits in `planner.candidates`
between due-today and due-this-week and has never once fired. The day is ordered by due
date and age alone, so docs/04's "commitments against goals" join does not happen.

The cause is throughput: `checkpoints.link_commitment` is the only writer, takes one
commitment and one goal, and is reachable only from `backglass goals link`. Nobody links
132 rows one CLI call at a time.

**A word-overlap suggester was built to fix this and then deleted, because it does not
work.** Recorded here so nobody builds it twice. The goal vocabularies are rich and
well-written — 53 words under the med school goal once its roadmap steps are folded in,
including "clinical", "exposure", "coursework", "prerequisite". The commitments are short
and concrete. The overlap between them is empty by construction:

```
Submit Hospice of the Valley volunteer application  → hospice, valley, volunteer
Lock Willow Hall 502 move-in slot                   → willow
Get into a competitive med school (+ its roadmap)   → clinical, exposure, documented, …
```

Over the whole ledger it produced one suggestion, and that one was coincidence
("accept McKenna Program admission offer" ↔ med school, on the shared words "offer" and
"program"). The owner writes goals as outcomes and promises as actions, and no amount of
token matching crosses that. Lowering the floor buys noise, not recall.

There is no deterministic path either: extraction never sets `goal_id`, and roadmap steps
do not create commitments, so nothing in the ledger already knows the answer.

- **Implement**: a `goal` field on the extraction, chosen from the active goal list passed
  into the prompt. The model already has the source text in hand and the list is eight
  items — this is an extraction field, not a retrieval problem, so it stays inside
  docs/02's architecture and nowhere near a vector store. Low confidence goes to the
  review queue like everything else (rule 2), which is also where a declined suggestion
  gets remembered.
- **Cost**: this is item 12's re-extraction, and lands with it.
- **Test**: an at-risk goal pulls its linked work above due-this-week items; a fixture
  where the source names no goal proposes none rather than the nearest one.

### 7. Estimates are guesses, so the packing is a guess

112 of 132 estimates are the 45-minute type default; 3 are extracted, 17 manual.

**No code needed, and the original proposal here was wrong.** `estimates.ratio_report`
does not want a new writer: it already derives actual from a completed block's own span,
joined to the commitment's estimate. It is starved, not broken.

```
plan_block outcomes: {'pending': 172, 'rolled': 4}
ratio_report sample: 0 of the 30 §1.3 requires
```

Not one block has ever been marked done. Which is item 3, arriving from the other end:
the evening pass — the surface that asks what got done, prefilled and one tap to confirm
— ran at 18:00 while the owner's own gym routine starts at 17:30. It asked an empty desk
every night for the life of the ledger, so nothing was ever confirmed, so the estimates
never calibrated and the rollover counts stayed near zero too.

- **Implement**: nothing. `141eba6` moves the pass to 22:00; the sample accrues from
  there. Revisit after 30 completed blocks, which is the first time the report can say
  anything at all.
- **Depends on**: re-running `backglass schedule install` from the real checkout, or the
  job keeps its old 18:00 plist.

---

## Tier 3 — smaller, real

### 8. Routines cannot be scoped to a weekday

`ROUTINES` parses `name@HH:MM+MINUTES` and applies every entry to every day. Banner
volunteering is Wednesdays 4–8pm and collides with gym, shower and dinner. The workaround
is a calendar event, which capacity already subtracts.

- **Implement**: optional day scope, `name@HH:MM+MINUTES@wed`.
- **Test**: a Wednesday-only routine costs no capacity on Tuesday.

### 9. `state` does not hash the frozen templates

`FROZEN_SURFACES` covers `dashboard.css` and `tokens.css`. The sidecar also freezes
`web/templates/`, so a stale frozen template is invisible to the command CLAUDE.md says
to trust before blaming the app. This was live during the 2026-08-09 lane fix: the CSS
was reported stale and the templates, equally stale, were not.

- **Implement**: hash the templates directory into `FROZEN_SURFACES`.
- **Test**: a modified template shows up in `stale_surfaces`.

### 10. "46 items did not fit" is compliant and useless

P2 forbids silent truncation, and the plan obeys it by printing 46 lines, many of them
restatements of each other. Fixing #4 shrinks this on its own; until then the overflow
should group rather than enumerate.

### 11. Beyond four concurrent events the timeline columns stop being readable

Fixed in `dbb1f8f` up to any number of lanes, but a quarter-width block already
ellipsizes its title. Worth a cap plus an overflow affordance if a day ever holds six.

---

### 12. The ledger predates the fixes to the code that fills it

The concurrent session's work is all at extraction time: prompt v8 turns "be somewhere at
8am" into an engagement, engagements collapse on overlap plus shared words, all-day plans
render as banners. None of it has touched a single existing row. Measured after merging
that work:

```
132 open commitments, 48 visible to the planner, 1 goal-linked, 112 default estimates
6 move-in engagements, 5 with no start hour
```

`prompts.versions_in_the_ledger` is `extract-commitments@7`; on disk it is `@8`. So the
owner's own move-in still has its hour trapped in a title, and will until the rows are
re-read.

- **Implement**: `backglass extract` re-runs against the new prompt without re-fetching.
  Scope: **1,041 kept items still on `@7`** (224 are already `@8`), not all 9,067.
- **Test**: none new. The extraction fixtures are the coverage; this is an operation.

**Priced, because rule 7 makes the cap a code-enforced constraint and this is the only
item on the list that meets it.** From `model_call`, 60 real extract calls at
`tier='extract'`: mean **$0.1264** and **21.9s** each.

```
1,041 items × $0.1264  ≈ $132 imputed      monthly_spend_cap_cents = 5000 ($50)
1,041 items × 21.9s    ≈ 6.3 hours serial
```

The dollars are not real. `MODEL_BACKEND=claude_cli`, so cost is **imputed from token
counts against a subscription that bills none of it** — the failure already recorded for
2026-08-03, where the cap froze extraction over money nobody was charged. What is real:

- **The cap will trip at roughly item 400** and degrade the rest to triage-only, silently
  producing a partial re-extraction that looks finished. Raise it for the run or the run
  is a third of a run.
- **~6 hours and the subscription rate limit**, which is the half of the 2026-08-03 issue
  that was never fixed.
- **The batch lane is not available here.** `schedule.py` is explicit that the overnight
  Batches jobs "need a real Anthropic API key — the subscription CLI backend cannot drive
  them", so the usual half-price answer does not apply.

Do item 6's goal field in the same pass. Re-reading 1,041 items is the expensive part and
it is identical either way; adding a field to the prompt while it happens costs nothing
extra, and doing them separately means paying the 6 hours twice.

---

## Tests to add, collected

| Test | State |
|---|---|
| no two timeline entries share a lane and a minute | done, `dbb1f8f` |
| an engagement whose text states an hour is placed at that hour | done by the other session, `2af4508` |
| several restatements of one engagement collapse to one row | done by the other session, `46a8fd5` |
| the shutdown hour derives from the working window | done, `141eba6` |
| a stale frozen template appears in `stale_surfaces` | done, `078b301` |
| each duplicate pair names its counterparty; a fan-out is labelled | done, `35f7414` |
| a weekday-scoped routine costs no capacity on other days | done, `58c5f93` |
| the overflow partitions into named and counted, losing nothing | done, `34b48dd` |
| an at-risk goal pulls its linked work up the order | **open** (item 6) |
| a logged actual reaches `estimates` and moves the type default | not needed — `ratio_report` already derives it (item 7) |
