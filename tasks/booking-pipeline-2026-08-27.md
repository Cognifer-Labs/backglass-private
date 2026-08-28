# A booking is not homework — 2026-08-27

## The ask

*"continue to improve backglass so it goes through a similar thought process pipeline as
you just did"* — said immediately after a session that booked a Dreamscape Learn VR pod
session for CHM 113 by hand.

So the specification is that session's own reasoning, written down and then made
mechanical where it honestly can be. What it actually did, in order:

1. Read a Canvas assignment and noticed the deliverable is **not work**. "Schedule Here!
   Lab 2 Module 1 — Act I" asks the owner to go to an external system and reserve a
   seat. Ninety minutes of "assignment" is fifteen minutes of booking plus a fifty-minute
   appointment on a day the ledger did not yet know about.
2. Derived the **operative deadline from the description, not the due date**. Canvas said
   Sep 3 at 11:59pm. The description said *complete it before coming to lab*, and the lab
   meets Thursday 8:00am on Sep 3, so the real cutoff was sixteen hours earlier than the
   date on the row.
3. Enumerated the candidate times the external system offered.
4. Filtered them against every fixed thing on the day — six courses, two labs, a pending
   volunteer shift — with the walk between buildings counted.
5. Booked, verified server-side, and wrote the result back to the calendar, the ledger
   and a reminder.
6. Noticed the **next two things were blocked**, each with its own date: Act 2's signup is
   locked until Sep 3, and BIO 181's pod signup sits behind a prerequisite quiz and closes
   Sep 4.

Step 3 and step 5's booking half need a browser and a human, and stay out. Steps 1, 2, 4
and 6 are the pipeline, and the ledger can do all four.

## What is already true, so it does not get rebuilt

- **Estimation.** `coursework.py` reads real numbers off assignment text and 172 open
  items carry `estimate_source = 'analyzed'`. Nothing here retunes a type default.
- **Conflict filtering.** `plan/capacity.py` already models fixed events, routines and —
  since the 2026-08-27 travel work — the walk between buildings. Step 4 above was done by
  hand with the same numbers `capacity.compute` already holds. The calendar event booked
  in that session landed as `source_item 11161` and `day_events(2026-09-02)` placed a
  15-minute walk in front of it with no prompting.
- **Submission state.** `canvas_enrich.py` (103c0c9) already imports what Canvas knows
  from a document the owner exports from their own signed-in session. This extends that
  lane; it does not open a new one, and it does not turn it into a connector — the
  docstring forswears that in terms this file will not relitigate.

## The gap, measured

**A. A booking-shaped obligation is stored as prose and treated as homework.** Eighteen
open `i_owe` commitments are appointment-shaped — schedule, sign up, book, reserve, RSVP,
appointment. Nine of them carry **no due date at all**, which is the whole of what the
planner knows about them:

```
509  RSVP for today's AI Scholars Welcome event at 3:30 p.m.        (no date)
353  schedule mandatory first-year honors advising appointment      (no date)
217  Schedule appointment with pre-med advisor                      (no date)
265  Confirm move-in appointment/arrival details in Housing Portal  (no date)
```

`509` names a time inside its own sentence and still resolves to nothing. Across the whole
ledger **101 of 378 open `i_owe` commitments have no due date**, so 27% of the board is
invisible to a planner that sorts by deadline.

**B. The operative deadline is not the due date, and the phrase that says so cannot be
found.** Assignment 30's description contains the sentence that moved the real cutoff:

```
must complete your VR Pod Experience **before** coming to lab, during its assigned week.
```

`instr(description, 'before coming to lab')` returns **0**. The markdown emphasis markers
sit inside the phrase. A naive matcher over this corpus finds nothing and says nothing —
the exact silence the 2026-08-27 lesson names as the one failure mode neither the ledger
nor the owner can see.

**C. The ledger cannot say "not yet".** Act 2's Canvas page says *locked until Sep 3 at
12am*, available through Sep 15, due Sep 10. The ledger holds one date per commitment.
There is no column for "cannot be started before", so a gated item is either absent or
falsely due, and the only reason the owner knows about Sep 3 at all is that a session
opened the page and read it.

**D. Work that is already on the calendar is budgeted twice.** Commitment 588 is open at
90 minutes due Sep 3. The work it names is the pod session now fixed on the calendar for
Sep 2 at 5:50pm. `select` has no link between a commitment and the event that *is* it, so
the Sep 2 plan will propose ninety minutes of pod session on top of the pod session. That
is the same class of leak as the 2026-08-27 two-phase lesson: minutes spent in one phase
that the other phase cannot honour.

**E. A manual entry is re-read by the extractor.** `backglass add` wrote commitment 639
at 23:59 against a manual `source_item`. At 00:21 a sync produced commitment **642 from
the same `source_item`** — `type_default`, confidence 0.95, and a due date of Sep 10 taken
from prose inside the note rather than the Sep 3 the entry stated. One source item, two
open commitments, and the model's reading beat the owner's. This is a rule 3 violation
sitting underneath everything else here and it is cheap to fix.

## The shape

Four increments. They are ordered so each is separately shippable and separately
revertible, because launchd runs this checkout every thirty minutes and a half-applied
migration takes the installed app down with it.

**Boundary, stated once.** Nothing here books anything. The pipeline's output is *"this
is a booking, the window opens here and closes there, and these hours of yours are free
inside it"*. Clicking Reserve stays an interactive-session act with the owner present.
A system that reserves seats on its own is a different product and needs a different
conversation.

### Increment 0 — a manual entry is the owner speaking (gap E)

- Mark manual source items so triage/extraction skip them. The `source = 'manual'` row is
  already distinguishable; the extractor is simply not asking.
- Supersede 642 into 639 rather than deleting it — the ledger keeps history.
- Test: `add` then `sync` against a frozen fixture writes exactly one commitment.

### Increment A — booking detection and the operative deadline (gaps A, B)

- `backglass/booking.py`. Deterministic, layer-1 in `coursework.py`'s sense: no model.
  - **Markdown-tolerant matching.** One normaliser that strips emphasis, collapses
    non-breaking space and folds whitespace before any pattern runs, and a test whose
    fixture is assignment 30's real sentence with the `**` in place. Gap B is the
    regression test, not a footnote.
  - Recognises the booking verbs in a title (`Schedule Here!`, `Sign up`, `Reserve`,
    `RSVP`) and the appointment nouns in a description (`pod session`, `appointment`,
    `show time`, `reservation portal`).
  - Emits a `Booking` record: the act of booking (small, minutes from the existing type
    table) and the thing attended (the assignment's own effort estimate), so the ninety
    minutes stops being one lump.
- **Operative deadline.** Where the description says *before class* / *before coming to
  lab* / *prior to your section*, resolve against the course's own meeting time and take
  the last meeting at or before `due_at`.
  - **The meeting-time source is the `fact` table, not `calendar:asu`.** Written on the
    premise that the `calendar:asu` row for CHM 113 lab still said Thursday 18:00–19:50 in
    PSD 232 against the owner's own Thursday-morning correction. **Checked against the
    live ledger during the build, and that premise had expired**: the evening rows are
    retracted and corrected 08:00 PSD 228 rows exist. The trap did not go away, it changed
    shape — both rows are still there on the same dates, and 18:00 sorts after 08:00, so
    "the last meeting before the due date" returns the retracted one unless retractions
    are honoured. The fix is two guards, not one: filter `source_item_retraction`, and let
    a structured `fact` override outrank the calendar where the owner has not retracted
    anything.
  - When fact and calendar disagree, **say so and fall back to `due_at`**. Rule 5: a
    failing input degrades and is surfaced, it does not guess. The disagreement goes on
    the Sources panel, not into a silent date.
- Reconciliation, per the 2026-08-27 lesson: count assignments matching the booking shape
  against the records emitted, and name the difference. A detector that finds nothing must
  say it found nothing.

### Increment B — dates the ledger cannot hold (gap C)

- Migration (**check the number in the worktree at build time — 0035 landed today**):
  `commitment.not_before` and `commitment.window_closes_at`, both nullable. NULL means
  "no gate", which is every existing row, so behaviour is unchanged by the migration
  alone.
- `canvas_enrich.py` gains `unlock_at` / `lock_at` off the same browser export it already
  reads. Same document, same owner-in-the-loop import, no new lane.
- Planner: an item whose `not_before` is after the day being planned is **not a candidate
  and not overflow**. "Not yet" is a third answer and the runway should print it as one,
  otherwise a gated item reads as a failure to place every morning until it unlocks.
- `window_closes_at` earlier than `due_at` outranks the due date for priority — a signup
  that closes Sep 4 for work due Sep 7 is due Sep 4.

### Increment C — the work is already on the calendar (gap D)

- `commitment.scheduled_source_item_id`, nullable, pointing at the calendar event that is
  this obligation.
- `select` does not budget a commitment that has one; the day shows the fixed block and
  the commitment rides on it. When the event's day passes, the shutdown pass asks whether
  it happened rather than rolling ninety minutes forward.
- Set it by hand for 588 as the first case, and from Increment A's `Booking` record
  thereafter.

## Sequencing and risk

- 0 and A are additive reads and one skip rule — no migration, no planner change.
- B is the migration and the only one that can take the installed app down. It ships
  alone, with `backglass app-update` in the same breath.
- C changes what the planner budgets, so it ships last and behind a test that asserts the
  day's total is *lower* by exactly the linked commitment's minutes.

## Out of scope, deliberately

- Auto-booking, per the boundary above.
- A Canvas connector on the session cookie. `canvas_ics.py` and `canvas_enrich.py` both
  refuse this in their own words; the enrichment stays a document the owner exports.
- The BIO 181 acknowledgement quiz. Submitting an attestation on the owner's behalf is
  not a pipeline feature.
- `tasks/todo.md` — a concurrent session is building the Homework tab and owns that file
  along with `homework.py`, `web/routes/homework.py`, `courses.py`, `web/app.py` and
  `base.html`. Nothing here touches any of them. This work is on
  `worktree-booking-pipeline`.

## Steps

- [x] 0.1 Manual source items excluded from all three pending queries; regression test.
- [ ] 0.2 The 39 already-open duplicates. **Not done, and not doable from here.** The fix
      stops new ones; the board still carries the old ones. Cleaning them changes what the
      owner reads every morning, so it goes through them — see "What is still owed".
- [x] A.1 `booking.py` normaliser + the markdown fixture from assignment 30.
- [x] A.2 Booking detection, split into book/attend records.
- [x] A.3 Operative deadline off `fact`, with the retraction filter that turned out to be
      what actually made it right; conflict surfaced when the calendar disagrees.
- [x] A.4 Reconciliation counts, printed by `backglass bookings`.
- [x] B.1 Migration 0036: `assignment.unlock_at`, `assignment.lock_at`. **On `assignment`,
      not `commitment`** — same institution, same browser export, and the join through
      `source_item` already exists. Two writers for one fact was the alternative.
- [x] B.2 `canvas_enrich.py` reads them; the docs/07 snippet now exports them.
- [x] B.3 Planner: "not yet" is a third answer, and a window that shuts before the due
      date outranks it. Ranked on, not displayed as — `Candidate.closes_at` carries the
      earlier date so a surface can say where it came from.
- [ ] B.4 `app-update`. **Deliberately not run from the worktree**: it would build and
      install the desktop app from a branch, and the installed app should follow the
      shared checkout. Run it after merge, in the same breath as the merge.
- [x] C.1 Migration 0037: `commitment.scheduled_source_item_id`.
- [x] C.2 A linked commitment is not a candidate;
      `test_the_day_loses_exactly_the_linked_minutes` measures the reduction.
- [x] C.3 `booking.past_events` + the shutdown question. Asks, never closes.
- [ ] C.4 **Nothing sets the column yet.** Linking 588 to the Sep 2 event is a one-line
      UPDATE; having `booking.scan` propose links from a `Booking` and its calendar event
      is the next increment, and it wants the owner's eye on the first few matches before
      it is allowed to write.

## What is still owed, stated plainly

**The 39 duplicate commitments.** They came from the bug Increment 0 fixed and they are
still open, still holding planner minutes. The pairs are findable — a manual `source_item`
carrying both the owner's own row (`estimate_source = 'manual'`, confidence 1.0) and one or
more model-made rows beside it:

```sql
SELECT s.id AS source_item, c.id, c.what, c.due_at, c.estimate_source, c.confidence
FROM commitment c JOIN source_item s ON s.id = c.source_item_id
WHERE s.source = 'manual' AND c.status = 'open'
ORDER BY s.id, c.confidence DESC;
```

Superseding rather than deleting keeps the history, and the resolution note should quote
which row it lost to. It is a small script and it is the owner's call, not a side effect
of a bug fix.

**No live `meeting:` override is written.** The mechanism is in `booking.py` and tested;
the CHM 113 case that motivated it no longer needs one, because the stale calendar rows
are retracted and the corrected ones are in. Writing an override nobody needs would be a
second source of truth for a fact already settled. The grammar is documented for the next
time a course moves and the calendar has not caught up.

**`backglass bookings` reports; it does not write.** Commitment 588 still carries its 90
minutes and the calendar event booked for Sep 2 is still budgeted separately. That is
Increment C, and until it lands the board double-counts that one obligation.
