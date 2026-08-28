# The scheduler surfaces say things the data does not — 2026-08-27

## The ask

*"continue to audit and improve basic display pipeline for scheduler and longterm
scheduler"*.

Two surfaces, four displays: `backglass plan` and `/schedule` for the day; `backglass plan
--runway` and `/schedule/runway` for the semester. Everything here is **display only** —
no allocator, no capacity model, no migration. Every number below was measured against a
copy of the live ledger (`schema_version 35`, untouched; the worktree carries unapplied
0036 and 0037 and must not be pointed at the real database).

## A — the runway headline contradicts the rows directly beneath it

`/schedule/runway` opens with:

> **The board clears on Fri 13 Nov — 241 obligations, 185h, every one of them with a day.**

and then draws **44 rows** carrying a `Won't fit` chip. The page asserts completeness and
immediately lists the exceptions.

Measured through `runway.solvency` on the same inputs the route uses:

```
len(sittings)          = 241    <- the headline's count
len(unreachable)       = 44
unreachable ∩ sittings = 5      <- in the headline's 241 AND chipped "Won't fit" below
unreachable − sittings = 39     <- absent from the headline entirely
beyond                 = 0
```

Two separate faults in one sentence:

1. **Five obligations are counted as having a day and rendered as not having one.** They
   are partially placed — real information, and `solvency` is right to keep it — but
   "every one of them with a day" is false of them.
2. **Thirty-nine are not in the count at all.** The board is 280 obligations; the headline
   says 241 and calls that all of it. The missing 39 are precisely the ones that need a
   decision.

`_echo_runway` prints the same claim (`"all with a day"`) from the same numbers.

## B — the advice sentence names the wrong horizon, 44 times

Every unreachable row renders:

> *90m left, and the next **14** days have nowhere to put it before it is due — start it
> early, cut its scope, or move the date.*

The route walks `runway.solvency`, which goes to the **last deadline on the board — 78
days**, not fourteen. `horizon_days` is passed as `runway_mod.DEFAULT_HORIZON_DAYS`, the
constant, rather than the horizon that was actually used. The number is wrong on all 44
rows, and it is wrong in the direction that understates the problem: "we only looked two
weeks ahead" is a much softer claim than the truth, which is that eleven weeks of capacity
were walked and the work still does not fit.

This is the 2026-08-27 lesson — *two questions, two horizons, each stated* — reappearing
one layer up. The computation was fixed; the sentence describing it was not.

The identical bug is latent in `_echo_runway`, in the `beyond` branch. It did not fire on
this ledger only because `beyond` is empty over a horizon chosen to reach every deadline.
Fixed in the same pass so it cannot surface later as a regression.

Cost, too: the sentence is 140 bytes repeated 44 times — 6.2KB of a 92KB page, and the
differentiating information (which obligation, which date, how many minutes) is buried in
between restatements of one piece of advice.

## C — the day header reports zero for a day full of fixed events

`backglass plan` on 2026-08-27, run after the working window closed:

```
2026-08-27 (America/Phoenix)  capacity 0m of 0m (fixed 0m, buffer 0m, travel 0m, reserve 45m)
  8:00am–9:50am  CHM 113 (Lab) [fixed]
  10:30am–11:45am  HON 171 [fixed]
  12:00pm–1:15pm  PSY 101 [fixed]
```

`fixed 0m` sits directly above 110 minutes of fixed lab. The arithmetic is not wrong —
`capacity.compute` clamps the window to what is left of the day, and nothing is left — but
the header is describing *remaining* capacity while the list under it describes *the whole
day*, and nothing says so. `reserve 45m` surviving the clamp while every other component
zeroes is the tell.

The same contradiction exists mid-day in smaller form: at 17:22 the breakdown counts only
the events still ahead.

**Fixed narrowly**: when the clamp has closed the window, the parenthetical breakdown is
replaced by the reason. The fuller fix — compute the breakdown over the whole day and the
capacity over what remains — changes every header the owner has ever read and is deferred
rather than done quietly.

## D — a derived walk offers Done, Roll and Pin

The 15-minute travel blocks the planner inserts carry the full action set:

```html
<div class="ev k-fixed tiny act" title="10:15am–10:30am Walk to Tempe WILOHAL 112">
  <span class="et">10:15am</span>
  <div class="evacts">
    <button …/blocks/1565/outcome/done">Done</button>
    <button …/blocks/1565/outcome/rolled">Roll</button>
    <button …/blocks/1565/pin/1">Pin</button>
```

`Entry.actionable` is `block_id is not None or source_item_id is not None`, and a walk has
a real `plan_block` row, so it qualifies. But a walk is not an obligation: it exists
because two rooms are far apart. "Roll to another day" would move a consequence of
geometry to a day whose geometry is different, and `Entry` already carries `travel` to say
so. The dataclass's own comment states the principle it then misses — *"a routine is
configuration with no row behind it and gets no buttons rather than a button that lies."*

## Evidence, not fixes

Two things the audit surfaced that are already owed elsewhere. Recorded so the next reader
does not re-diagnose them.

**The duplicate commitments are visible on the runway.** "Complete AI Scholar Welcome
Survey" and "Complete the AI Scholar Welcome Survey" both land on Mon 31; four ASU Ready
variants share Fri 04, two of them byte-identical. That is the 39 open duplicates from the
`quick_add` re-extraction bug (`e494f17`) reaching a surface. The fix is the cleanup that
commit's message defers to the owner — **not** display-side dedupe, which would hide a
ledger problem behind a renderer.

**The runway schedules a booked pod session for the day after it happens.** `due
2026-09-03  Schedule Here! Lab 2 Module 1 - Act I  Thu 03·90m` — the session is reserved
for Wed 2 Sep 6:00pm, and its operative deadline is the Thu 8:00am lab it must precede.
Wrong day, wrong minutes, work already on the calendar. Increments A and C on this branch
fix exactly this; it is live proof for the merge, not a second thing to fix in the
renderer.

## Steps

- [x] A. Runway headline reconciles, in `_reconciled` on the web side and inline in
      `_echo_runway`, from the same three numbers. Both now print
      *"clears on Fri 13 Nov for 236 of 280 obligations — 185h with a day; 44 do not
      finish before they are due (5 part-placed and still short)"*, and 236 + 44 = 280.
      The plain sentence still renders when nothing is unreachable — the reconciliation
      is not a permanent hedge, and a test pins that too.
- [x] B. The advice sentence is the unreachable section's header, once, and names the
      horizon the walk actually covered: *"The 44 below have all 78 days to their deadline
      and still do not finish."* Rows carry due, title, minutes and any partial placement.
      `DEFAULT_HORIZON_DAYS` is no longer passed to either renderer. Page went 92,053 →
      87,472 bytes; the sentence went from 44 occurrences to 1.
- [x] C. Closed-window header prints *"the working window has closed — the day below is
      done"*. Extracted to `_capacity_line` so the branch is testable without freezing the
      clock in a CLI runner. A zero window with nothing under it keeps the breakdown —
      there is nothing for it to contradict.
- [x] D. `Entry.actionable` is False for travel. The walk block now renders as
      `class="ev k-fixed tiny"` with no `act` and no buttons.
- [x] E. `_ellipsis` on both truncating columns.

Verified against a copy of the live ledger before and after, plus 8 new tests.

## Not in scope

`dashboard.css` and `base.html` belong to the concurrent Homework-tab session. Everything
here is template text and route context; if a fix needs CSS it is the wrong fix.
