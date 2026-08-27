# The Schedule page becomes something you can act on — 2026-08-27

## Why, in the owner's words

"Chem lab is no longer 6 to 7:50 Thursday, why hasn't backglass backend updated to show
that" — followed by "everything in schedule should be clickable and interactable".

The second sentence is the fix for the first. The lab moved on 2026-08-23, the ledger
*knew* (facts 63 and 85, read off MyASU), and the day page went on drawing a 6:00–7:50 pm
block anyway — because the planner reads calendar rows and the only door to a wrong
calendar row was a Python script. Every surface in this product can be corrected from the
UI except the one the owner looks at every morning.

## What was already fixed, before any of this (done)

`calendar:asu` is a one-shot registrar import fetched 2026-07-31 that no connector
re-reads, so `retraction.py` can never certify a window over it — those rows were
permanently uncorrectable by machinery. Fourteen evening-lab rows from 2026-08-27 forward
retracted with an owner-correction reason; fourteen morning rows (Thu 8:00–9:50,
PSD 228, class #90619) inserted, derived from the retracted rows' own dates so the
Thanksgiving skip came along for free. The 2026-08-20 row is left standing: that lab
really did meet in the evening. `fact` 48 superseded by 98 — its note still carried the
old grid.

## The obstacle

`Entry` — what the timeline actually draws — has no id. `_raw_entries` flattens two
readers into `RawEntry` tuples of `(start, minutes, title, kind, outcome, travel)` and
`_collapse` merges the copies. Identity dies there, in a merge that was written to make
one event draw once. Nothing downstream can name the row it came from.

## Steps

1. `plan/capacity.py` — `FixedEvent` carries `source_item_id`. `fixed_events` already
   selects from `source_item`; it selects `si.id` too. Routines and engagements have no
   calendar row and leave it None.

2. `web/routes/schedule.py` — `RawEntry` becomes a frozen dataclass rather than a
   six-tuple. It is unpacked in five places and about to grow two fields; a tuple that
   wide is a positional bug waiting to happen.

3. Same file — `_collapse` merges ids the way it already merges `outcome` and `travel`:
   the plan copy is the only one that knows `block_id`, the calendar copy is the only one
   that knows `source_item_id`, and the merged entry keeps both. This is the existing
   rule ("fields are merged rather than taken from a winner"), applied to two more fields.

4. `Entry` carries both ids through to the template.

5. `web/actions.py` — `retract_source_item`. Writes one `source_item_retraction` row with
   an owner-marked reason. It is the door that was missing: additive, reversible by
   deleting one row, and it never touches the immutable `source_item`.

6. `web/routes/schedule.py` — a `_timeline.html` partial and POST routes that re-render
   it, so an action swaps the canvas rather than reloading the page. Same idiom as
   `_today.html`: HTMX, `hx-target` on the panel, buttons in the tab order.

7. `schedule.html` — per-entry actions, chosen by which id the entry has:
   - a plan block: Done · Roll · Pin, reusing `/blocks/{id}/…` unchanged;
   - a calendar event: a link to `/source/{id}` (rule 1 — provenance is one click away)
     and "Not happening", which retracts;
   - a routine: no id, no buttons. Inert rather than given an endpoint that lies.
   Every entry gets a whole-block click target so a 20px block is still reachable.

8. Week view: entries there are 20px tall and cannot hold buttons. Each links through to
   its day. Stated rather than silently narrowed.

9. Tests: `_collapse` merges ids; the retract action writes one row and is idempotent;
   the page renders buttons for a block and not for a routine — sliced with
   `conftest.py::panel_slice`, never `.split()`.

## Not in scope

A MyASU connector. The durable fix for the class schedule is the owner putting the lab in
Calendar.app, where `calendar:apple` plus the certified-read retraction already maintain
it. The rows inserted today are another orphan set — a correct one. That is the owner's
call to make, not this task's.
