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

## The Sources panel

Looks like ops chrome, is actually the most important panel.

Per source: status square (green healthy, vermilion failed), name, relative timestamp of
last successful sync. On failure, the row's keyline turns vermilion and stays that way
until fixed.

Also show the triage kill rate as a percentage. If it drops below 85 percent the rules
have drifted and cost is about to climb.

## Interaction rules

- Every commitment card is a click target in full, not just its title.
- Optimistic UI on ticks and resolutions, with rollback on failure.
- No confirmation dialogs except for drop, which is destructive and rare.
- Keyboard: `j`/`k` to move between cards, `x` to resolve, `r` to open review queue.
  This is a tool used daily by one person who will learn the keys.

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

- No shadows, no rounded corners except the 2px reel digit windows.
- Section headers are solid black bars with cream uppercase condensed text.
- Every colored fill carries a black keyline.
- Three chart series maximum; single-series charts use black, not cobalt.
- Tabular figures everywhere.
- Reel digits on at most three numbers per view.

`design/preview.html` is a rendered reference with realistic data in both modes. Match it.
