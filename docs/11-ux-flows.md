# UX Flows

Nine flows. Each lists the steps, the states that can occur, and what the system must
never do.

The governing principle across all of them: **the system proposes and reports; the owner
decides.** Every flow that could plausibly end in the system taking action on its own
ends in a proposal instead.

---

## 1. First run

Once, at setup. Command line, not a wizard, because there is one user and a config file
is faster than a UI nobody will see twice.

```
backglass init
```

1. Create the database, run migrations.
2. Prompt for the **data boundary decision** (`docs/08`). This is first, before any
   credential is entered, so it is impossible to ingest before deciding. Refuse to
   continue without an explicit choice.
3. OAuth each connector in turn, browser flow, tokens into the `credential` table.
4. Working window, peak window, week start, timezone, brief delivery time.
5. Prompt to enter 3 to 7 goals with definitions of done. Skippable, but the prompt
   explains that goals are hand-entered by design and will never be inferred.
6. Run `sync --dry-run` automatically and print what it would have captured.

**Never:** ingest anything before step 2 completes. **Never:** default the boundary to
"include everything" if the user hits enter.

---

## 2. Morning brief

The core loop, five days a week.

```
05:45  plan generated
06:00  brief sent
~07:00 owner reads it on a phone, in bed or over coffee
```

1. Email arrives. Subject is the date and the one thing that matters most today, not a
   generic "Your morning brief."
2. Owner reads. Two minutes, under 400 words.
3. Any line can be tapped through to its source: the Gmail thread, the Drive doc, the
   note. This is the only navigation the brief has.
4. A "Open dashboard" link at the bottom, once, not per section.

**States:**

| state | what the brief leads with |
|---|---|
| normal | Today's plan |
| timezone changed overnight | the change, working window in both zones |
| a source failed | "Gmail auth expired 3 days ago, this brief is incomplete" |
| spend cap hit | "Pipeline degraded to triage-only since 14 Jul" |
| day fully booked | "No deep work block available today, calendar is fragmented" |
| generation failed | a short failure notice, never silence |

**Never:** send two emails on a Monday. Monday planning replaces the brief rather than
arriving alongside it.

---

## 3. Resolving a commitment

The most frequent interaction, and the one that must be fastest.

1. Owner opens the dashboard, or taps through from the brief.
2. Commitment board, grouped by status, swimlanes by counterparty.
3. Click the card body to expand: full extracted text, source link, estimate, linked goal.
4. Resolve, drop, or snooze. One click, no confirmation except on drop.
5. The card re-renders in place as done. No page reload, no toast.
6. Tomorrow's brief reflects it.

Keyboard: `j`/`k` between cards, `x` resolve, `d` drop, `s` snooze, `/` search. Learn once,
use daily.

**Never:** a confirmation dialog on resolve. The action is reversible and the dialog costs
more over a year than the occasional misclick.

---

## 4. Review queue

Where trust is earned or lost.

1. Low-confidence extractions land here, never in the brief.
2. Each shows the claim the model made, then the exact source sentence beneath it in
   quotes, then Accept and Reject as equal-weight buttons.
3. Accept promotes it to a normal commitment. Reject tombstones it so re-extraction does
   not resurrect it.
4. Rejecting also records the reason category, one click: wrong date, not a commitment,
   not mine, already done. Four buttons, no free text.

Those categories are the feedback loop. After fifty rejections, the distribution tells you
which part of the extraction prompt to fix.

**Never:** make Accept the primary button, style it larger, or preselect it. The whole
point of the queue is an honest judgment, and a nudge toward accepting turns it into a
rubber stamp.

---

## 5. Evening shutdown

Optional, small, and skippable without consequence.

1. At the end of the working window, one notification.
2. Three fields, all prefilled: what got done (from the day's plan), what rolls over, one
   free-text line on anything learned or blocked.
3. Submit, or ignore.
4. If ignored, the planner infers completion from ledger state and marks the rest rollover.

**Never:** nag. No reminder, no "you missed your shutdown," no streak penalty. A
productivity system that scolds gets deleted, and this is the flow where that temptation
is strongest.

---

## 6. Monday planning

Replaces the brief. Slightly longer, still under 500 words.

1. Last week's targets, hit and missed, with counts.
2. **Capacity check**: committed hours against available hours. When over, the gap is
   named in hours and targets are listed by cost, largest first.
3. Goals at risk two weeks running get one direct question: extend the date, cut the
   scope, or raise the weekly target. Three links, each of which does the thing.
4. Targets missed three weeks running are flagged unrealistic, with a one-click "lower
   this target" that is framed as a legitimate outcome rather than a failure.
5. Commitments aging past 14 days with no movement.

**Never:** drop a target automatically. Name the gap, offer the lever, let the owner pull
it.

---

## 7. Friday retrospective

Appended to the normal Friday brief. Under 150 words.

1. What completed this week, grouped by goal.
2. Estimate versus actual, one line.
3. Anything that rolled over three or more times.
4. One prompt: what is the single thing that would make next week better. Free text,
   stored, surfaced in Monday's planning.

That last item closing the loop into Monday is what makes it worth writing.

---

## 8. Source failure and recovery

The flow that keeps the system honest about its own gaps.

1. A connector fails. Status goes `failed`, error recorded.
2. Sources panel: the row's keyline turns vermilion and stays that way.
3. The next brief leads with it by name and by how long it has been failing.
4. The dashboard offers a "Reconnect" button that runs the OAuth flow inline.
5. On success, the next sync backfills from the stored cursor, so nothing in the gap is
   lost.

**Never:** let a failed source degrade quietly. The dangerous failure is not the error, it
is a brief that looks complete and is not.

---

## 9. Travelling

Phoenix to Coimbatore and back, several times a year.

1. Owner sets the active timezone with a date range, explicitly. Not geolocated, because
   IP inference is wrong exactly when it matters.
2. On the first day in the new zone, the brief leads with the change and shows the working
   window in both zones.
3. Brief delivery time follows the active zone, so 06:00 stays 06:00 local.
4. Meetings scheduled with counterparts in the other zone display both times.
5. Checklist streaks compute per local day in the active zone. A timezone change never
   retroactively breaks a streak.

**Never:** schedule a work block at 03:00 local because the window was computed in the old
zone.

---

## Cross-cutting rules

1. **Provenance everywhere.** Every claim in every surface links to its source. No
   exceptions, including in the review queue and the Monday planning.
2. **Proposals, not actions.** The planner never writes to the real calendar. The system
   never sends email as the owner. Nothing is resolved on the owner's behalf.
3. **Empty states are declarative.** "Nothing open." Not "You're all caught up!"
4. **Failures are louder than successes.** A working sync produces one log line. A failed
   one reaches the brief, the dashboard, and the exit code.
5. **No dark patterns, including friendly ones.** No streak guilt, no urgency copy, no
   preselected accept, no "are you sure you want to skip?"

---

## What has no flow, deliberately

- Onboarding tour, tooltips, feature discovery. One user who built it.
- Notifications beyond the brief and the shutdown prompt.
- Sharing, export, collaboration.
- Settings UI. Config is a file; editing it is faster than any form.
- Search. If you are searching the ledger, the brief has failed at its job, and the fix is
  the brief rather than a search box.
