# Homework tab — a month calendar of what is due and what is on (2026-08-27)

Owner's ask, mid-session: *"create a home worjk tab that shws a calendar that shows when
each event and home work due or at"*. Two things on one grid: the events the day already
holds (classes, labs, meetings) and the work that is due on it.

## Why it is not the Schedule page

`/schedule` and `/schedule/week` answer "what am I doing in the next hour" — a timeline
of placed blocks over one day or seven. Neither answers "when is everything due", which
is a month-shaped question and the one a student actually asks. The month grid is a
third register, not a wider week: it draws due dates, not durations.

## What it reads

Nothing new is stored. Two readers over tables that already exist:

- **Due** — open (and this month's completed) `commitment` rows with `direction='i_owe'`,
  joined through `source_item` to the `assignment` row behind them when there is one, for
  the course code, the Canvas URL and the effort estimate.
- **On** — `capacity.day_events`, minus routines and walks: calendar events and confirmed
  plans, deduped across `calendar:asu` and `calendar:apple` the same way the planner sees
  them. One builder, so the calendar cannot disagree with the day plan.

## The reconciliation the 2026-08-27 lesson demands

> "for anything derived, count the input rows against the output rows — silence is the
> one failure mode neither the ledger nor the owner can see."

An assignment with a due date and no commitment behind it is invisible on every existing
surface. The page counts assignments due this month against the commitments backing them
and lists the difference by name, under the grid. The same check, run live against the
Canvas API during this session, found ten CHM 113 Laboratory assignments the `canvas:ics`
feed never delivered — so the count is not hypothetical.

An unlinked assignment is also drawn on the grid, in its own register, rather than being
left off: a page that silently omits it repeats the failure it exists to report.

## Steps

- [x] 1. `backglass/homework.py` — `Due`, `Event`, `Day`, `Month`, `load(conn, settings, first)`.
- [x] 2. `backglass/web/routes/homework.py` — `/homework`, `?month=YYYY-MM`, `?only=coursework`.
- [x] 3. `homework.html` + the month grid in `dashboard.css`, tile register §7b.
- [x] 4. Nav entry in `base.html`'s one `pages` list; router in `app.py`.
- [x] 5. Tests: month arithmetic, the unlinked count, tz correctness across the grid edge.

## What building it found

The reconciliation was written to report a gap and immediately reported none: 72 Canvas
assignments in September, 72 with commitments, 0 without. The gap was upstream of the
count, where it could not see it — checked against the Canvas API through a signed-in
Safari tab, CHM 113 Laboratory had 13 upcoming assignments and the ledger held 3.

**Root cause, in `connectors/canvas_ics.py`.** Canvas publishes an assignment whose due
date belongs to the owner's own section under `UID:event-assignment-override-<id>`, and
publishes it *instead of* the base `event-assignment-<id>` rather than beside it.
`ASSIGNMENT_UID = r"event-assignment-(\d+)"` wants a digit where `override-` is, so
`_parse` returned None and the assignment was dropped — not its date, the assignment.
Twelve of them: ten of CHM 113 Laboratory's graded work for September, and two CHM 113
Recitation activities. Nothing failed. The connector reported `ok`, `upstream_count`
counted what it had parsed, and the feed's 219 assignments against the ledger's 207 was
a comparison no surface made.

Fixed by recognising the shape and keying on the assignment id recovered from the
VEVENT's own URL (`…#assignment_7494004`) — the override id names the override, so keying
on it would fork one assignment into two immutable rows the day its section date moved.
`tests/test_canvas_ics.py::TestSectionOverrides` holds all four properties.

After the sync: 220 assignments, 12 new commitments, none to the review queue. The
September audit line now reads 82 / 82 / 0, and this time it is true.

**Still open, and the feed cannot fix it.** The ICS document carries no submission state,
so two of the recovered items — Individual Prelab Quiz 1 (4.5/5) and the Individual
Safety QUIZ (8.8/10) — are graded in Canvas and open in the ledger. That is the
documented downgrade in this connector's own module docstring, not a new bug.

- [ ] 6. `backglass app-update` so the desktop app carries the new template. Needs a
      clean tree, so it waits on the commit.

---

# Making the writes feel instant (same day, second ask)

Owner: *"make the app more responsive: when i drop some thing it should dissappear among
other basic animation need to be added"*.

**What was measured before changing anything.** Drop is two clicks (confirm.js arms the
first), and after the second: a POST, then a re-render of `#panel-board` — the largest
fragment in the product — then an `outerHTML` swap of the whole panel. `/` renders in
260ms and weighs 425KB with 1185 `hx-post` controls on it. So the card the owner just
dropped sat there for a quarter second, and then the entire board replaced itself. The
write was never slow in the sense that matters; it was slow in the only sense the owner
can see.

**What was added.** §9 mechanism 8 — the row leaves at the click, and the write goes on
underneath it. Optimistic **view**, never optimistic **record**: the request is still
sent, the panel still swaps, and the swap still wins every disagreement. A refused write
puts the row back and `oops.js` says why.

Marked with `data-vanish` in the template, never inferred from the URL: Resolve and Drop
on a commitment card, Received on an awaiting row, every answer in the review queue, and
a roadmap's unlog ×. **Snooze deliberately carries none** — it moves a commitment to
tomorrow and the card stays on the board, so vanishing it would show a row leaving and
the swap putting it straight back.

**This amends §9's first omission**, which read "Nothing leaves". The reason it gave was
latency — an exit inside the swap makes htmx wait, and `defaultSwapDelay` is 0 for
exactly that reason — and that reason is untouched: `.htmx-swapping` still carries no
transition and `test_nothing_animates_on_the_way_out` still holds it. This exit runs
*before* the request, in time the owner was going to spend waiting anyway. The boundary
that replaces the ban: **an exit may run off the critical path and nowhere else.**

## Two defects found by review, both real

1. **`hidden` did not hide.** The attribute's `display:none` is in the UA stylesheet, so
   `.row{display:flex}` and `.card{display:flex}` beat it. The row went to zero opacity
   and kept its space; the gap closed only when the swap landed, and under
   `prefers-reduced-motion` — where the fade is skipped on purpose — the click produced
   no feedback at all. Measured in a headless browser both ways: without the rule the
   hidden row still occupied 136px and pushed its neighbour from 148 to 296.
   `[hidden]{display:none !important}` in dashboard.css, with
   `test_the_hidden_attribute_actually_hides` holding it.

2. **One `pending` slot restored the wrong row.** Two exits are in flight whenever the
   review queue is worked in streaks, and a write landing during the half-hourly sync
   waits on the lock (the 2026-08-11 lesson). The second click overwrote the slot, so a
   refusal resurrected whichever row was in it — one row back that was gone, one still
   missing that the ledger holds open. Now a `Set`, and `restore` takes the row from the
   failing request's own `detail.elt`. Verified live: server killed, two Resolves
   clicked, both rows returned, ledger unchanged.

## Verified live, not just tested

Against a scratch ledger, driving Safari: Drop → the card goes and the ledger says
`dropped`; server killed → Resolve → the row comes back with "▲ Not saved" and nothing
written. 27 motion tests, full suite green.

## Known leftover

The sidebar's open-commitment count is rendered on page load and is not in any swapped
fragment, so it is stale until the next navigation — before this change and after it.
Worth fixing as an out-of-band swap; not part of this.

---

# Wiring the month to everything beside it (same day, third ask)

Owner: *"continue to add all needed features to link homework tab with others"*.

The month shipped reachable from the sidebar and from nowhere else, and it pointed at
the day and at Canvas and at nothing in between. Four joins, each of which answers a
question the owner was already asking somewhere else.

## 1. The course, as a filter and as a link

`?course=CHM+113` narrows the month to one course — **its deadlines and its meetings**,
because a student asking what CHM 113 wants from them this month means the lecture, the
lab and the recitation. `homework.subject` normalises the three spellings a link might
carry (`CHM 113`, `chm113`, `CHM113`) through the same regex `courses._subject` built the
label with, so the two surfaces cannot come to disagree about which class a piece of work
belongs to.

The chip strip reuses the Activity page's `.afilter` rather than inventing one; it is the
same object, plain links whose state is the URL, so a filtered month can be bookmarked.
The filtered course stays on the strip even when the month holds none of it — a strip
that vanished on an empty month would strand the owner inside a filter with no way out.

Classes now points here twice: the per-course assignment count is the link (it was a
readout of a number nothing could be done with), and the "due next" lane names the month.
The month points back at `/classes#panel-course-<slug>`.

## 2. Whether the planner has actually made room for it

The link the page exists to make. Owner, 2026-08-27: *"other assignments havent been
scheduled"* — true at the time, and unanswerable, because a due date and a plan lived on
two surfaces that never referred to each other. A deadline is a claim about when work is
owed; a block is a claim about when it happens, and only the second one gets it done.

Every due item that a live plan holds a block for now carries the day it sits on
(`▸8 Sep`), and a panel under the grid says how many have one and lists the ones that do
not, each linking to the day it is due.

**Bounded by the planner's own horizon, and the bound is the finding.** The planner
proposes one day at a time at 05:45, so on 27 August it has reached 8 September. Counting
"unplanned" past that would report the whole of next month as unscheduled every time the
page was opened — the windowed-measurement failure of 2026-08-27 in a new place. The
panel says the horizon, counts only up to it, and states in words that work past it is
*unconsidered, not unscheduled*. On the live ledger: 18 items on August have a block, 33
due on or before the horizon do not.

`status != 'superseded'` throughout — the predicate `planner.current_plan_id` and the
brief read a day's plan with. A replanned day leaves its old rows behind, and counting
them reports work as scheduled on a day whose plan no longer exists.

## 3. Day, week, month — each names the other two

`/schedule` and `/schedule/week` gained a `month` link in the pager they already had;
the month gained `day` and `week`. Three registers of the same question, and until this
line the third was reachable only from the sidebar.

## 4. The page tells the truth about what it is showing

Both counts under the grid follow the filter. A month narrowed to CHM 113 that went on
reporting all 82 of the month's assignments would be answering the question the owner had
just navigated away from, and two panels saying "this month" while only one of them meant
it is a page disagreeing with itself.

## Two defects the tests caught

`keep` was built with `&amp;`, and Jinja escapes a variable on the way out — so the href
read `&amp;only=` and the browser sent a parameter literally named `amp;only`. The filter
looked right in the source and dropped on the first click of the pager. The test now
follows the link rather than reading it.

An empty filtered month drew forty-two empty framed cells and three zeroes, which reads
as broken rather than as nothing due. docs/06 §Empty states, with the way out of the
filter in the sentence.

## Checked, not assumed

`homework.subject` was run over every distinct calendar title in the live ledger: seven
subjects, all real courses, no room code or meeting title parsed as one.

---

# The ledger could not change its mind about a fact (2026-08-27)

Owner: *"i no longer have trayfavors, the app should know this, why does it not process
things like these"*.

It knew, twice over, and could act on neither.

## What the record actually held

`fact` 59 — `work/team-joined` — said "Joined Tray Favors team December 2025", status
active. It was extracted on 2026-08-23 from source item **10576, the email in which the
owner asked to leave Tray Favors.** The poison gate behaved exactly as designed: the
sentence was quoted verbatim, the confidence cleared `AUTO_ACCEPT_CONFIDENCE`, the lane was
known. The fact was true when it was written; the background sentence in a mail about
leaving was read as a statement of current standing, which is what it looked like.

`fact` 86 — `premed/clinical_volunteering` — said the placement-change reply was "PREPARED
2026-08-24, reply drafted in Mail but **NOT YET SENT**". It was written at 10:52. The reply
went out at 10:58 (items 10576, 10765) and the coordinator answered three times that
afternoon (10757 with the adult openings list, 10781 saying she would chase the ED training
schedule, 10782 asking for the ED service description). Four days later the ledger still
said it was sitting in a draft.

The thread itself was processed correctly — three open commitments track the transition
(521 change-request form, 522 the update Aimee owes him, 523 the service description) and
522 is even the right direction, `owed_to_me`. Nothing was missed at ingest. What was
missing is anything that reads a standing fact again.

## The gap, stated exactly

An obligation the record has overtaken has three mechanisms: `logic` throws out what is
structurally contradicted, `extract/relevance` retires what a recorded fact makes moot,
`extract/recheck` re-reads a chat commitment against what the conversation said next. All
three consume facts as *evidence*. None of them judges a fact.

`questions._contradictions` is the nearest thing and cannot fire here: it needs the same
`(subject, key)` written twice with two different values, and nothing ever wrote a second
one. The fact went stale in place, which is the one shape that surface cannot see.

And it is worse than an inert row. `facts.owner_context` rides into every model call, so
since the 24th the product has been telling itself, on every triage, every extraction and
every plan, that the owner is on Tray Favors with an unsent draft.

## Fixed now, by hand

`work/team-joined` → 107, and `premed/clinical_volunteering` → 108, both through
`memory set`, which supersedes rather than edits. ED is recorded as **in progress, not
confirmed** — Aimee is still waiting on their training schedule, and writing it as settled
would be the same class of error one step later. `~/.claude/.../banner-placement-change.md`
said "unsent draft" too and was corrected the same way.

## Built so the class self-reports

`backglass revise` — migration 0036, `specs/extraction-prompts/revise-facts.md`,
`backglass/extract/revision.py`, wired into `sync` after `relevance` and printed in the
sync summary.

It is handed the active facts and the newest kept items, and asked one question per fact:
**has something since made this untrue?** Batched, judged once, ids in and ids back, and a
citation or no verdict — the four properties `relevance.py` and `recheck.py` already share.

**One thing is inverted, and it is the whole design.** `relevance` may drop an obligation
on its own above a threshold, because a wrong drop costs one row the owner can see is
missing and re-add. This pass **may never write anything active, at any confidence**: a
wrong fact rewrite is a bad premise under every model call that follows and it compounds
silently. Every verdict lands as an ordinary `proposed` fact, on the Memory page that
already exists, accepted through `facts.accept` — one door for "a fact becomes current",
and the owner's click is it.

Two guards worth naming. `overtaken` must quote the sentence that overtook it, from an item
the pass actually sent, and the quote is checked against that item — silence is not
evidence, and neither is age. And a `replacement` is required: "no longer true" is a
deletion wearing a verdict's clothes, so the model must say what is true *now*, keeping the
history ("joined in December 2025 **and** left in August 2026") rather than erasing it.

The recurrence key is `(fact_id, through_item)` — the newest item the judgement saw. A
watermark on the run would re-judge all 72 facts every sync and re-pay for the same
answers; a watermark on the fact alone would judge it once and never look again, which is
the failure being fixed. Keyed on the pair, a fact is re-judged exactly when the ledger has
read something since anyone last considered it.

16 tests, including the Tray Favors case end to end and the assertion that matters most:
after a confident `overtaken` verdict, the original fact is still `active` and
`owner_context` is unchanged until the proposal is accepted.

## Does it actually work — measured, not assumed

Built, then run against a scratch ledger seeded with the two facts exactly as they read
before they were fixed, plus the seven real messages of the Banner thread copied out of the
live ledger.

**On the configured free-tier model (`nvidia/nemotron-3-super-120b-a12b:free`): 3 judged,
0 proposed.** Including the `NOT YET SENT` fact that rule 7 was written for, with the sent
reply sitting two lines above it in the same prompt. The first theory was the render: the
author column held a bare address, so "this address is the user, therefore the user sent
it, therefore it is not a draft" was three inferential steps to reach something the ledger
knows for certain. Prompt v2 marks the owner's own items `YOU (the user wrote this)` and
rule 7 says what that means. Re-ran: **still 0 proposed.**

**On `claude_cli`, same ledger, same prompt: 1 proposed, 2 current, 0 discarded, 34c.**
The revision is the right one — `premed/clinical_volunteering`, cited to Aimee's own reply,
replacement saying the request was sent. And the two `current` verdicts are right as well:
`work/team-joined` really is a permanent statement about December 2025, and nothing in that
thread says the placement ended — that one was only wrong because the owner said so out
loud, which no pass can read. The ASU fact is untouched.

So the mechanism, the prompt and the guards work, and the free tier is not strong enough
for this judgement. Two consequences worth saying rather than discovering later:

- Running on the free tier, this pass will mostly return `current` and cost nothing. It is
  wired into `sync` anyway because it is harmless there — it writes nothing active — and
  because the fallback chain (`MODEL_FALLBACK_BACKEND=claude_cli`) already promotes work
  the free backend refuses.
- `backglass revise` run by hand with `MODEL_BACKEND=claude_cli` is the way to get a real
  pass today, and 34c for the whole knowledge base is the price.

The prompt's `model: careful` frontmatter is a hint nothing consumes — `run()` passes
`settings.model_extract`, exactly as `check-relevance` does. Left consistent with its
sibling rather than special-cased here; if the tier hint is ever honoured it should be
honoured for both.

## The bug the first real run found

The pass went live inside half an hour without being asked to: launchd runs `sync` from
this working tree, so the scheduled run picked up `_revision_pass` and judged 52 facts on
the free backend before anyone typed the command. (`tasks/lessons.md`, 2026-08-27: the
scheduler runs the checkout.)

Every one of those verdicts was formed against the wrong evidence. `evidence()` ordered by
`occurred_at DESC`, and `occurred_at` is when an item *is about* — for a `canvas:ics` row
that is its **due date**. So "the newest forty things the ledger has read" was in fact "the
coursework due furthest in the future", topped by an excused-absence form due in **March
2027**, and every fact in the ledger was being judged against a list of deadlines that have
not happened.

It showed up as a bad proposal rather than as an error, which is the only reason it was
caught: the one revision the pass offered cited *"Self & Team Evaluation is due
2026-12-06"* as grounds for rewriting the semester's course-load fact. A citation that
cannot support its verdict is exactly what the citation rule exists to make visible, and
here it made the *window* visible instead.

Two fixes, both in `evidence()`:

- **Order by `id`, not `occurred_at`.** Insertion order is what "newest read" means, and
  it is what `through_item` is a watermark on — so the window and the recurrence key now
  describe the same thing. They did not before.
- **Nothing dated in the future is evidence.** A thing that has not happened cannot have
  overtaken a fact. Filtered on the date rather than the source, because the shape is what
  makes it useless and not which connector wrote it: a calendar event next month is
  exactly as inert as a Canvas deadline.

The window is now recent mail, messages, reminders and calendar rows — the things that
actually say a state has changed. The 52 cached verdicts were deleted rather than kept:
they are a cache of judgements formed on evidence that was never evidence, and leaving them
would have suppressed the fixed pass on exactly the facts it was built for.

Three tests hold it, including the one that ties the watermark to the window it was formed
from.
