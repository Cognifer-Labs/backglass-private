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

