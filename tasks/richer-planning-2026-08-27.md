# A day that holds the real work — 2026-08-27

Four asks, in the owner's words:

1. "we need it to be even more rich, for example reading for human event and other
   assignments havent been schedul[ed]"
2. "if things dont fit remove relax time and mcat study time or reduce time for other things"
3. "for example there are 2.25 hours available that can be planned for"
4. "also account for travel and walk to classes"

## What is actually wrong, measured

**A — 22 assignments never became commitments.** `assignment` holds 205 rows; 183 have a
commitment behind them and 22 do not. Every one of the 22 was triaged `keep` and run
through `extract-commitments@10` — the model read them and emitted nothing. They are real
graded work with a due date and an effort estimate already computed: Week 3, Week 4,
Week 11, Week 12, Week 14 of HON 171, both Participation Grades, six PSY 101 practice
quizzes, seven LearningCurves, two CIS 236 practice sets, a CHM 113 VR pod session.

The design flaw behind it: a Canvas assignment is a commitment **by construction** —
Canvas states the deliverable, the deadline and the course — and it was being made to
depend on a language model's judgement about prose. Rule 2 says a low-confidence
extraction goes to review; this said nothing at all, and silence is the one outcome
neither the ledger nor the owner can see.

**B — the Human Event readings do not exist anywhere.** The HON 171 syllabus was ingested
(`source_item` 10397) and extracted, and produced four commitments: two essays, "post
weekly reading response on Canvas" with no date, and "bring the Norton Anthology to
class". The syllabus's actual Course Schedule holds **28 dated readings**, one per class
meeting — Gilgamesh 9/1, the Bhagavad-Gita 9/8, Oedipus 9/17, the Aeneid 10/6, Beowulf
10/20, Dante 11/10 — and not one of them is in the ledger. The Canvas side only ever has
the 150-word discussion post (19 minutes) that says "the assigned reading" without ever
naming or timing it.

**C — nothing yields.** The backlog is 255 hours against a day of ~2–4 free hours, and the
planner's only answer to work that will not fit is to say so.

**D — walking is free.** `capacity` models travel, and only for a calendar event whose
`raw_json` carries `travel: true`. The `calendar:asu` class rows do not, so both measured
days report `travel 0m` — a Monday that runs CIS 236 (BA 396) → BIO 181 (MUR 101) →
CHM 113 (LSA 191) → LSB 191 (ARM L1-17) across the Tempe campus costs zero minutes of
walking, and the gaps between those classes are offered to the planner as workable time.

**On (3), the 2.25 hours.** Today's strip read "2h 25m available · 2h 23m planned" — a day
that is already nearly packed. The systemic answer is A, B and C rather than a hunt for
one unspent slot: A and B put the missing work in front of the planner, C gives it
somewhere to go.

## The one that needs care: what "remove relax time" actually means

Relax is a routine at `relax@21:00+120` and the working window is `07:00-21:00`. **Relax is
entirely outside the window and costs zero capacity today.** So "remove relax time" is not
a matter of freeing minutes inside the day — it is *extending the day into the evening*,
and that is a bigger thing to do to somebody than the sentence sounds. MCAT is not a
routine at all: it is goal target 1 (5 × 95m practice sections a week) and 24 (5 × 30m
foundation), and no MCAT block has ever been placed in 94 days of plans.

So C is: **under deadline pressure, the day may extend into the hours the sacrificial
routines hold, and those routines yield.**

**The trigger is the discriminating part.** "If things don't fit" cannot mean the backlog,
or Relax never happens again this semester — 255 hours will never fit. It fires only for
work that is *deadline-pressed*: overdue, due today or tomorrow, or named by
`runway.unreachable` as not finishing before it is owed. The runway built earlier today is
what makes that distinction possible.

**Yield order, and the floors.** Stated here as assumptions rather than asked, because the
owner named the first two and the rest follow from what they did not name:

| | |
|---|---|
| Relax | removed first — named, and outside the working day |
| Study / MCAT | removed second — named |
| Gym | compressed, never removed, and not below half |
| Breakfast, lunch, dinner, shower | untouched. Not named, and a planner that eats meals to fit a quiz is one that gets turned off |
| Sleep | untouched. The window may reach 23:00 and no further |

P18 applies unchanged: **a routine that yields is stated, never silently dropped.** The
plan must say "Relax gave up 60m to the CIS 236 team charter", or the owner finds out by
noticing their evening is gone.

## Build order

Deterministic first, policy change with its tests third, new extraction last.

1. **A — promote assignments to commitments directly.** An `assignment` row with a due
   date is an obligation; it should not need a model to agree. Check each candidate
   against open commitments by fuzzy title before inserting — the same quiz may already
   be on the board from an apple-mail Canvas notification — and send anything ambiguous
   through `duplicates`, never a bulk insert.
2. **D — synthesise travel between adjacent classes in different rooms.** The mechanism
   exists; the rows lack the flag. A flat walk-minutes knob, not geocoding: a default
   nobody measured is honest, a distance matrix invented from building codes is not.
3. **C — the relief pass.** Plan the day normally; if deadline-pressed work is still in
   overflow, recompute with relief and re-place. First pass stays the default, so a day
   that fits is untouched.
4. **B — the reading schedule.** A new extraction over the syllabus's Course Schedule,
   producing one dated obligation per class meeting, with the syllabus as provenance
   (rule 1). Fixture set from the real text, per the testing rules; no live API.

## Not in scope

Per-reading page-count estimates. "Vol. A, pp. 885" is a start page, not a length, so a
per-reading default knob is the honest number and a computed one would be fiction.

## Status, 2026-08-27 evening

**A — done and applied.** `coursework.promote_orphans` promotes a dated Canvas assignment
with no commitment behind it, deterministically, at confidence 1.0. Run against the live
ledger: 22 promoted, 0 orphans left, open commitments 405 → 427. Hooked into `sync` ahead
of `apply_estimates` (it creates the rows those then price) and counted as
`assignment_promotions`, which should read zero on every future run — anything else means
the extractor read a Canvas assignment and said nothing again.

The near-duplicate guard matters and its shape was measured: a title-only check called 8
of the 22 duplicates and **every one was wrong** — "Chapter 4 Practice Quiz" and
"Chapter 14 Practice Quiz" share five of six words and are five weeks apart. With the due
date in the identity, the same run found zero. Anything that does match is skipped for
`duplicates` to judge, never merged here.

**D — done.** `capacity.walks_between` synthesises travel before a class whose room
differs from the one before it, sized to the gap and never larger. Monday now costs 45
minutes of walking across four buildings and capacity fell 285 → 225 before relief. Room
names compare on words minus "Tempe", so the registrar's "Tempe PSD 228" and
Calendar.app's "PSD 228" are one room and buy no walk.

**C — done, graduated.** Relief fires only for deadline-pressed work — overdue, due today,
or named by the runway as not finishing in time — never for the backlog. Level 1 extends
the day to 23:00 and removes Relax; level 2 additionally cuts the gym to half. **Each
level has to pay for itself**: if the relieved plan leaves no fewer deadline-pressed items
than the cheaper one, the cheaper one stands. Without that, three duplicate welcome
surveys that are unreachable at every level would have cut the gym every day for the rest
of the semester and placed nothing extra — which the first run did.

Measured on 2026-08-31: Relax gives up its 120m, the day runs to 11:00pm, the gym keeps
its full hour, and both facts are stated on the plan.

**B — done and applied.** The Human Event readings. See below.

## B, when it is picked up

The shape is unusually favourable and worth stating before anyone reaches for a model.
The syllabus's Course Schedule is highly structured — `WEEK 6  Tu 9/22:  Sophocles,
Oedipus Tyrannos` — so a deterministic parser is testable against the real text, costs
nothing, has no confidence to threshold and cannot hallucinate a reading that is not
assigned. The two-tier extraction exists for prose; this is a table that lost its lines in
a PDF.

What it must produce: one obligation per class meeting that names a reading, due on the
meeting date, `source_item` 10397 as provenance (rule 1), a per-reading default estimate
knob rather than computed page maths — "Vol. A, pp. 885" is a start page, not a length,
and a number derived from it would be fiction.

What it must skip: the meetings that are not readings — "Workshop: thesis statements",
"Fall Break", "Thanksgiving Break", "Course Reflections" — and the essay deadlines, which
`extract-commitments@10` already found (commitments 504 and 505).

## B, as built

`backglass/syllabus.py`. A parser, not an extraction: the Course Schedule is a table that
lost its lines in a PDF — `WEEK 6  Tu 9/22:  Sophocles, Oedipus Tyrannos` — and the model
had already had its turn at this document and returned four rows out of thirty. Parsing is
testable against the real syllabus, spends nothing on the cap, has no confidence to
threshold and cannot invent a reading nobody assigned.

**Applied: 22 readings written**, Gilgamesh through Chaucer, `source_item` 10397 as
provenance and the table row as the quote. Ten meetings correctly skipped and named —
six workshops, two breaks, Course Reflections, and the two essay deadlines
`extract-commitments@10` had already found. A second run wrote zero (rule 3).

Estimates are a flat `reading_minutes = 90` knob. A syllabus cites where a text *starts*
("Vol. A, pp. 885"), never how long it is, so a computed figure would be fiction wearing
arithmetic — and 90 minutes is what `max_block_minutes` already calls one sitting.

Two flaws the real text found that a hand-written fixture would not have: "Course
Reflections" slipped a `\breflection\b` filter, and the last meeting ran to the end of the
document and took the entire policies section with it as its title. Both are regression
tests now, and the fixture is cut to include the heading the parser must stop at.

## Two placement bugs the readings exposed

Adding the readings made a day so crowded that two long-standing defects became visible.
Both cost the owner real hours and neither is about readings.

1. **The small-items batch was charged for and then thrown away.** `select` spends budget
   on a batch of ten small obligations; `place` needs one contiguous run for it; the
   protected block and the day's real work take the long slot first; the batch fails and
   all ten go to overflow — *with their minutes still spent*. Measured on 2026-09-01: the
   day reported 565 minutes of capacity, placed 375, and left thirty items due that day
   unplaced. That is the "2.25 hours available that can be planned for" the owner was
   looking at. A batch of separate obligations is divisible by construction, so it now
   falls back to `place_split`. Same day, after: **545 of 565 placed**, due-today overflow
   30 → 20.

2. **Standing blocks outranked work due today.** Study and Coding are placed out of
   whatever `select` did not spend, regardless of what `select` refused. A 90-minute Coding
   block sat at 8:45pm while the Gilgamesh reading for the next morning's seminar was in
   overflow. They now stand down whenever deadline-pressed work is unplaced — which is the
   owner's own "reduce time for other things", applied to the two things on the day that
   nobody promised anyone.

## "continue to improve and fit everything into schedule" — the answer is that it does

Measured before changing anything: 449 open commitments, 291 hours; 213 of those hours
dated, 78 undated. Fortnight capacity, day by day through `capacity.compute`: **91.7
hours**, and the fortnight walk allocated **91.7 of 91.7** — saturated, with 109
obligations and 95 hours reported as `beyond`. That reads as a semester badly underwater.

It is not what it means. **The fourteen-day horizon was the thing failing, not the
semester.** Run the same allocator to the last deadline on the board — 106 days — and the
whole thing places: **263 obligations, 199 hours, clearing on 26 September**, with four
items left unreachable, all of them due the next morning. Nothing is beyond. The board
holds a month of work, not a semester of debt.

So the improvement is not a better packer. It is asking the right question:

- `runway.solvency` — the same EDF walk, run to the last deadline rather than to a
  fortnight, bounded at `MAX_SOLVENCY_DAYS = 400` (the owner carries a 2027 internship
  application and walking to it would compute a year of capacity for a question nobody
  asked). Two horizons because there are two questions: fourteen days is how far the
  *calendar* is real, and that is the right horizon for telling the planner what to start
  today; it is the wrong one for "does all of this fit".
- `runway.clears_on` — the day the last sitting lands.
- Both surfaced: the Schedule page's Runway panel and `backglass plan --runway` now open
  with *"The board clears on Sat 26 Sep — 263 obligations, 199h, every one of them with a
  day."*

The optimism is stated rather than hidden. Capacity past the sync window is the class
timetable and nothing else, so this is a **floor on infeasibility** — work that cannot fit
even here genuinely cannot fit — and a promise about nothing.

## Also fixed

**A duplicate-title collision this session introduced.** A seminar spends two meetings on
one text (Dante 10 and 12 November, Chaucer 17 and 19), and both became commitments named
"Read for HON 171: Dante Alighieri, Inferno". `backglass duplicates` scored that pair 1.00
and offered to drop one — deleting a real obligation. The meeting date is now part of the
name, the certain-duplicate count fell from 3 to 1, and the pairs are no longer
collapsible. The 22 existing rows were renamed in place.

**One genuine duplicate collapsed** (`duplicates --apply`): two copies of "upload ASU ID
photo and verify identity", one from Sun Devil Card Services and one from ASU.

## Two tests from a concurrent session, retargeted rather than deleted

That session added `Runway.beyond` — the fortnight's honest statement about its own edge —
with tests asserting the CLI and the panel both print "N obligation(s) has no day in the
next 14". Walking the whole board makes that line correctly empty, so both tests were
failing on a behaviour that had deliberately changed.

Their concern was right and is kept: far-future work must not become invisible. Both tests
now assert that property against the new mechanism — a November exam appears with days
against it — and `beyond` keeps its own direct test on the fortnight path, which is
unchanged and still correct.

