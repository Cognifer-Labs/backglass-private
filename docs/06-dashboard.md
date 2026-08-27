# Dashboard

One page, server-rendered, responsive. Opened during the day rather than pushed.

## Panels

| panel | content |
|---|---|
| Today | Proposed plan blocks, protected block marked, capacity line |
| Commitments | Board grouped by status, swimlanes by counterparty, drag to resolve |
| Awaiting others | Owed to you, sorted by age descending |
| Goals | Targets with weekly progress, staleness chips, risk projections |
| Checklist | Today's non-negotiables, binary ticks |
| Review queue | Low-confidence extractions, accept or reject |
| Sources | Last successful sync per source, item counts, triage kill rate, failures |

## Classes

A second read-only page, `/classes`, added 2026-08-21 when the owner's semester became the
thing the ledger mostly holds. One card per course: how it meets (pattern, room,
instructor, and the count of calendar rows that pattern was folded out of, so the line can
be re-derived rather than believed), what is due next with its effort estimate and the
materials it needs, the open commitments that belong to the course, and the archive
documents ingested for it.

It owns no table and stores nothing — `backglass/courses.py` reads `calendar:asu` rows,
the `assignment` tables, open commitments and `files` items, and joins them on the course
code. Two absences are stated on the page itself rather than left to be discovered: all-day
calendar rows never enter the ledger, and a timed exam only appears once it is inside the
calendar connector's 21-day horizon (docs/07 §Two things a calendar write does not put in
the ledger).

A class the calendar knows and Canvas does not still gets a card — LSB 191 has no Canvas
shell at all, and a page built from Canvas enrollments would have silently dropped a class
the owner attends every Monday.

## Activity

A third read-only page, `/activity`, added 2026-08-25. Three tables written between
migrations 0030 and 0032 had reached no surface at all: 421 claim events, 18 retractions
and 28 notifications, with no reader anywhere under `backglass/web`. Rule 1 asks every
claim to link to its source, and an audit trail the owner cannot open is a promise the
schema keeps and the product does not.

One list, three streams, newest first — not three lists, because three lists are what the
owner already had in three tables nobody could read:

- **change** — `claim_event`. What a typed record's field moved from and to, and the cause
  that moved it. The writers are the pipeline, not only the web layer: relevance
  re-judgement, the logic checker, fact supersession, upstream due-date moves.
- **retraction** — `source_item_retraction`. A row an upstream source stopped returning.
  This is the event 0030 exists to stop being silent, so it is the one stream that carries
  an ink on its keyline.
- **notice** — `notification`. What the system said out loud, and whether saying it worked.
  A banner recorded as `failed: <why>` is visibly different from a delivered one, because
  the ledger answers "what did the system tell the owner and when" and Notification
  Center's memory does not.

It owns no table and stores nothing. `backglass/activity.py` carries the merge; the page
is a router and a template.

**A change where the old value equals the new value is not a change.** 345 of the 421
claim events on the owner's ledger are `relevance_rejudged` rows recording `keep → keep` —
the re-judgement ran and agreed with itself. Listing them would bury the 76 rows that
moved under a 94% majority that did not. They are counted and named on the page rather
than dropped, because "nothing changed" is a real thing to have learned.

Filters are links, not controls: the state is the URL, so a filtered feed can be
bookmarked. An out-of-range window or an unknown stream falls back to the default rather
than refusing — this is a nav destination, and a page that 422s because a query string was
hand-edited is worse than one that shows the default and says which it used.

Times are UTC, stated on the page. The owner moves between UTC-7 and UTC+5:30 and a bare
"13:40" that silently means neither is a trap.

## Memory, and the state doc on it

`/memory` is the personal knowledge base: active facts grouped by the lane the owner filed
them under, plus the extraction candidates behind the poison gate (proposed facts, invisible
to `owner_context` until accepted).

Since 2026-08-24 it also carries the **Situation** panel — the state doc the owner asked
for: the same facts as a document with every line addressable (`[fact N]`), what changed in
the last sixty days with both values, what the week holds, and what the open board's
obligations rest on. It is a rendering, not a store; the fact rows below it *are* the store,
which is why it lives on this page rather than on one of its own.

Under it, "how it has changed" — the stored versions, each with the lines that appeared and
vanished to produce it. A version exists only where the document actually differed, so that
list is the evolution rather than a log of syncs.

**Opening the page never writes a version.** Versions come from the sync epilogue and from
`backglass situation --refresh`; a version per page view would make the history a record of
how often the owner looked. When what is on screen has not been stored yet, the panel says
so rather than implying it has.

## Write-back is required

Dashboard state changes write to the ledger. Resolving a commitment on the board marks it
done and tomorrow's brief reflects that. Ticking a checklist item persists.

**If the dashboard is read-only it becomes decoration within a week.** This is not a
nice-to-have; a surface you cannot act on is a surface you stop opening.

Actions that must write back:

- Resolve, drop, or snooze a commitment
- Accept or reject a review-queue item
- Tick or untick a checklist item
- Mark a plan block done or rolled
- Pin a block to a slot
- Edit an effort estimate
- Adjust a weekly target
- Accept the day's plan. The control is on the Today panel as well as `/schedule`: the
  plan is read on the dashboard, and with the button one page away `accepted_at` was NULL
  on all 88 day plans as of 2026-08-25.

## The Sources panel

Looks like ops chrome, is actually the most important panel.

Per source: status square (green healthy, vermilion failed), name, relative timestamp of
last successful sync. On failure, the row's keyline turns vermilion and stays that way
until fixed.

Also show the triage kill rate as a percentage. If it drops below 85 percent the rules
have drifted and cost is about to climb.

**The repair loop reports here too, when it fails.** `repair.on_open` runs on every app
open that finds the loop overdue, and it deliberately writes no `run` row — four readers
take the newest row with no `kind` filter and would report a repair pass as the last sync.
So its report went to stderr, which the desktop shell does not have: rule 5 asks a failing
part to degrade, be logged *and* be surfaced, and only the third was missing. The last
pass in the current process is held in memory and raises a vermilion sidebar alert when it
carries errors. Only failures — the loop runs on most page loads, and an alert per pass is
furniture inside a day. Nothing recorded means nothing has run yet in this process, which
is not the same as healthy and is not reported as such.

## Interaction rules

- Every commitment card is a click target in full, not just its title.
- Optimistic UI on ticks and resolutions, with rollback on failure.
- No confirmation dialogs except for drop, which is destructive and rare.
- Keyboard: `j`/`k` to move between cards, `x` to resolve, `r` to open review queue.
  This is a tool used daily by one person who will learn the keys.
- Page switching is the digits, in sidebar order. `base.html` emits the nav and the key
  map from **one** list — they were two hand-written copies, `/classes` was added to one
  and not the other on 2026-08-21, and every key from 3 on pointed one row above its label
  until 2026-08-25. Ten digits, thirteen pages: the tail (Activity, Ask, Scrub) is
  reachable from the nav and from the desktop shell's Go menu, never from a letter —
  letters belong to the dashboard and a global letter would fire there too.
- Every page with a route is in the nav. `/ask` and `/scrub` had routers, templates and
  tests and were linked from nowhere; semantic search could only be reached by typing the
  URL. A finished page that nothing links to is not shipped.

## Empty states

Every panel needs one, and the copy is declarative rather than cheerful.

- Commitments: "Nothing open."
- Awaiting others: "Nothing outstanding."
- Review queue: "Nothing to review."
- Goals with no targets: "No targets set. A goal without a target is inert."

Never "You're all caught up! 🎉". See `docs/05-morning-brief.md` §Tone.

## Visual specification

Everything visual comes from `design/design-system.md` and `design/tokens.css`. The
constraints that most often get violated:

- No shadows, and no gradients outside the protected hatch.
- Corners are rounded from the §7 scale — 2px marks, 4px controls, 6px containers — and
  nested radii are concentric (inner = outer − inset). The 2026-08-06 ruling retired
  "radius 0 everywhere"; §7 carries the scale and the list of what stays square.
- Section headers are solid black bars with cream uppercase condensed text.
- Every colored fill carries a black keyline.
- Three chart series maximum; single-series charts use black, not cobalt.
- Tabular figures everywhere.
- Reel digits on at most three numbers per view.

`design/preview.html` is a rendered reference with realistic data in both modes. Match it.
