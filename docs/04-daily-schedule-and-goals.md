# Daily Schedule and Goals

This is the part of the system that turns a ledger into a day. Everything else answers
"what do I owe and to whom." This answers "so what am I doing between 9 and 6, and is any
of it moving the things I actually care about."

The distinction matters because a commitment list that is merely accurate is still a
to-do app. The value here is the join: schedule against capacity, commitments against
goals, and intent against what actually happened.

---

## 1. The day planner

### 1.1 What it produces

Every morning the planner emits a **proposed day**: an ordered set of blocks covering
the working window, each block either a fixed obligation or a piece of discretionary work
drawn from the ledger.

The word "proposed" is doing real work. The planner never writes to the real calendar
without confirmation. It suggests; the owner accepts, edits, or ignores. A planner that
silently rearranges your calendar gets turned off in week one.

### 1.2 Capacity model

The single most common failure of automated day planning is proposing eight hours of work
into a day that has five hours of meetings. So capacity is computed first, and it is a
hard constraint rather than a display value.

```
working_window    = configured per weekday (default 09:00–18:00 local);
                    `weekend_window` overrides it on Sat/Sun when set
fixed             = calendar events marked busy, minus declined,
                    plus confirmed engagements with a stated hour
buffer            = 10 min after any meeting ≥ 30 min, 5 min otherwise
commute/travel    = any calendar event tagged travel, plus its buffer
capacity_minutes  = working_window − fixed − buffer − travel − reserve
reserve           = configured slack, default 45 min/day, never zero
```

**The reserve is not optional and defaults to non-zero.** A plan that fills every minute
is a plan that fails at 10:15 and stays failed. The reserve absorbs the first overrun.

**Engagements count as fixed, but only once they are three things: believed, confirmed,
and at an hour.** Below the confidence threshold a plan does not touch capacity at all —
CLAUDE.md rule 2 keeps a guess out of the brief, and letting the same guess silently
delete an hour from a real day is the same error with a heavier consequence, because the
brief at least renders nothing while the planner would quietly plan less work and never
say why. It goes to the review queue with the low-confidence commitments. A plan the owner agreed to occupies the day exactly as a meeting does — dinner at
seven is not time available for deep work — so it is subtracted through the same path
(`capacity.engagement_events`). The two exclusions are what keep that safe. A `proposed`
plan is not reserved, or anyone who emails the owner could delete an evening from their
week by suggesting one; it reaches them through the brief, which asks for a reply. A plan
with a day but no hour ("lunch on Friday") is not placed either, because there is no
honest hour to give it and an ISO date parses to midnight. Both stay visible in the brief.

| ID | Requirement |
|----|-------------|
| P1 | Compute `capacity_minutes` before selecting any work. Never select past capacity. |
| P2 | If selected work exceeds capacity, drop lowest-priority items and say so explicitly in the brief: "3 items did not fit." Never silently truncate. |
| P3 | If capacity is under 60 minutes, do not propose a plan. Say the day is fully booked and list only what is due. |
| P4 | Blocks have a minimum size of 25 minutes. Anything smaller is batched into a single "small items" block. |
| P5 | Never schedule across a fixed event. Never propose overlapping blocks. |

### 1.3 Effort estimates

Every open commitment carries `estimated_minutes`. Without it the planner cannot pack a
day.

- Extraction proposes an estimate when the source text supports one ("this'll take an
  hour," "quick review").
- Otherwise the default is by commitment type, configurable: review 30, draft 60,
  decision 15, meeting-prep 30, unknown 45.
- The owner can override on any item, and an override is sticky for that item.
- Track actuals. After 30 completed items, report the ratio of estimated to actual in the
  weekly review, and let the owner adjust the type defaults. Do not auto-adjust silently.

### 1.4 Deep work protection

| ID | Requirement |
|----|-------------|
| P6 | At least one contiguous block of ≥ 90 minutes is protected per weekday when capacity allows. |
| P7 | The protected block is placed in the owner's configured peak window (default 09:00–12:00). |
| P8 | Small items and admin never go in the protected block. |
| P9 | If no 90-minute gap exists, the brief says so plainly: "No deep work block available today, calendar is fragmented." That sentence is the product. Seeing it three days running is what prompts someone to change their meeting habits. |

### 1.5 Ordering within the day

Order is not by due date alone. The rule set, in precedence order:

1. Fixed events sit where they sit.
2. Anything overdue or due today goes before anything due later.
3. Blocked-on-others items get scheduled early in the day, so the ask goes out with a
   full working day left for a response. This is a real edge and worth the complexity.
4. The protected deep work block takes the peak window.
5. Everything else fills by priority, then by age.

Priority is derived, not entered: `overdue > due today > advances an at-risk goal > due
this week > everything else`. The owner can pin an item to a specific slot, and a pin
always wins.

### 1.6 Rollover

At the end of the working window, anything proposed but not marked done becomes
**rollover**.

| ID | Requirement |
|----|-------------|
| P10 | Rollover items appear at the top of the next day's proposal, above newly selected work. |
| P11 | An item that rolls over three times is flagged. The brief asks one question: is this actually going to happen, or should it be dropped? |
| P12 | Rollover count is stored per item and surfaced on the card. It is the single best signal of a commitment that needs renegotiating rather than rescheduling. |

### 1.7 Timezone and travel

Written for a user who splits time between two zones — worked example below uses
America/Phoenix (UTC-7, no DST) and Asia/Kolkata (UTC+5:30), a real pair chosen because
it has no DST on either side to complicate the math, but the mechanics apply to any
two-zone split.

| ID | Requirement |
|----|-------------|
| P13 | All timestamps stored UTC. The working window, brief delivery, and day boundaries follow the **active timezone**, not a fixed offset. |
| P14 | Active timezone comes from an explicit setting with an optional date range, not from IP geolocation, which is wrong exactly when travelling. |
| P15 | On a day where the active timezone changes, the brief leads with the change and shows the working window in both zones. |
| P16 | Meetings scheduled in the other zone display both local and counterpart time. A 09:00 Phoenix call is 21:30 in Kolkata, and getting this wrong once costs a meeting. |

### 1.8 Evening shutdown

A second, much smaller pass at the end of the working window. Not a second brief; a
prompt with three fields.

- What got done. Prefilled from the day's proposal, one tap to confirm.
- What rolls over. Prefilled, editable.
- One line on anything learned or blocked. Free text, stored as a note, feeds tomorrow's
  context.

Shutdown is optional and skippable. If skipped, the planner infers completion from
ledger state and marks the rest rollover. **Never nag about a missed shutdown.** A
productivity system that scolds gets deleted.

---

## 2. Goals

### 2.1 Structure

Three levels, no more. Deeper hierarchies get abandoned.

```
Goal        annual or quarterly, 3–7 active at a time, has a definition of done
  └ Target  weekly commitment, expressed as a countable
      └ Checkpoint   a dated event that advances the target
```

**Goals are entered by hand.** Do not infer goals from email. Inferred goals are
uniformly wrong and the owner is choosing perhaps eight things a year, so typing them is
not the bottleneck.

Every goal carries an explicit `definition_of_done`. A goal without one is a mood.

### 2.2 Weekly targets

A target is the weekly countable that a goal needs. Three kinds:

| kind | example | how it completes |
|---|---|---|
| **cadence** | "3 deep work sessions on NYWIC cutover" | count of sessions this week |
| **milestone** | "ship the schema migration" | boolean, dated |
| **maintenance** | "inbox to zero twice" | count, resets weekly |

| ID | Requirement |
|----|-------------|
| G1 | Every active goal has at least one target. A goal with no target is inert and the Monday brief says so. |
| G2 | Targets reset on the configured week start (default Monday, local). |
| G3 | Target progress is computed from checkpoints, never entered directly. |
| G4 | A target that has been missed three weeks running is flagged as **unrealistic**, and the weekly review asks whether to lower it. Lowering a target is a legitimate outcome, and framing it as one is the difference between a system that gets used and one that generates guilt. |

### 2.3 Capacity check

This is the requirement that makes goals honest.

Each cadence target has an implied weekly time cost. Sum them, compare against weekly
capacity from §1.2, and if the total exceeds available hours, **say so on Monday**:

> "Your weekly targets need 22 hours. You have 14 available after meetings. Something
> has to give."

| ID | Requirement |
|----|-------------|
| G5 | Compute committed hours against available hours every Monday. |
| G6 | When over capacity, name the gap in hours and list targets by cost, largest first. Do not auto-drop anything; the owner chooses. |
| G7 | When under capacity by more than 25%, say that too. Slack is information. |

### 2.4 Checkpoints and linkage

A checkpoint is a dated event that moves a target. Sources:

- A completed scheduled block linked to a goal.
- A completed commitment tagged with a goal.
- A manual entry.
- An extraction that references goal work, at confidence above threshold. Below the
  threshold it goes to the review queue like anything else.

| ID | Requirement |
|----|-------------|
| G8 | Commitments may link to at most one goal. Multi-goal linkage sounds useful and makes progress uninterpretable. |
| G9 | A checkpoint always records what produced it, with a source link where one exists. |
| G10 | Deleting a checkpoint recomputes target progress immediately. |

### 2.5 Staleness and risk

Two different signals, and conflating them is a common mistake.

**Stale** is about activity: days since the last checkpoint on any target of this goal.
Thresholds default to 7 days warn, 14 days serious.

**At risk** is about trajectory: at the current rate, will this goal reach its definition
of done by its target date? Computed as required-rate versus observed-rate over the
trailing four weeks.

A goal can be fresh and at risk (lots of activity, not enough to finish in time). It can
be stale and on track (ahead of schedule, took a week off). These need different
responses, so they get different treatments in the UI and different sentences in the
brief.

| ID | Requirement |
|----|-------------|
| G11 | Compute staleness and risk independently. Never merge into one "health" score. |
| G12 | Staleness shows as a chip with the day count: "12 days quiet." Never a bare color. |
| G13 | Risk shows as a projected completion date against the target date, in words: "on pace for 14 Oct, target is 30 Sep." |
| G14 | A goal at risk for two consecutive weeks gets one direct question in the Monday brief: extend the date, cut the scope, or raise the weekly target. |

### 2.6 Daily checklist

Separate from goals, and deliberately so. The checklist is the small set of daily
non-negotiables — the things that are habits rather than projects.

| ID | Requirement |
|----|-------------|
| C1 | Maximum seven items. The cap is enforced, not advisory. A checklist of twenty is a list nobody completes and the empty boxes become invisible. |
| C2 | Items are binary. No partial credit, no percentages. |
| C3 | Items may be weekday-scoped (weekdays only, specific days, daily). |
| C4 | Streaks are tracked and shown, but a broken streak resets quietly. No commiserating copy, no flame icons, no "you lost your 40-day streak." |
| C5 | The checklist appears in the brief only if incomplete items remain by the evening pass, and in the dashboard always. |
| C6 | Checklist state is per local day in the active timezone, and a timezone change does not retroactively break a streak. |

### 2.7 Weekly review

Two rituals, both generated, both short.

**Monday planning**, in place of the normal brief:

- Last week's targets, hit and missed, with counts.
- Capacity check for the coming week (§2.3).
- Goals at risk, with the direct question from G14.
- Targets flagged unrealistic under G4.
- Commitments aging past 14 days with no movement.

**Friday retrospective**, appended to the normal brief:

- What completed this week, grouped by goal.
- Estimate-versus-actual for the week, one line.
- Anything that rolled over three or more times.
- One prompt: what is the single thing that would make next week better. Free text,
  stored, shown in Monday's planning.

| ID | Requirement |
|----|-------------|
| W1 | Monday planning replaces the brief; it does not arrive in addition to it. Two emails on Monday means neither is read. |
| W2 | Friday retro appends and never exceeds 150 words. |
| W3 | Both are generated from the ledger with no manual input required. Input is optional enrichment, never a precondition. |

---

## 3. Data model additions

These extend the core tables in `docs/03-data-model.md`. Full DDL in `specs/schema.sql`.

```
goal
  id, user_id, title, horizon(annual|quarterly), target_date,
  definition_of_done, status(active|done|dropped|paused),
  created_at, closed_at

target
  id, goal_id, kind(cadence|milestone|maintenance), title,
  weekly_count, estimated_minutes_each, active, created_at

checkpoint
  id, target_id, occurred_at, source(block|commitment|manual|extraction),
  source_item_id NULL, commitment_id NULL, note, delta

checklist_item
  id, user_id, title, weekday_mask, active, sort_order

checklist_tick
  id, checklist_item_id, local_date, ticked_at

day_plan
  id, user_id, local_date, tz, capacity_minutes, generated_at,
  accepted_at NULL, status(proposed|accepted|superseded)

plan_block
  id, day_plan_id, starts_at, ends_at, kind(fixed|work|protected|small|buffer),
  commitment_id NULL, goal_id NULL, title, pinned,
  outcome(pending|done|rolled|dropped), rollover_count

shutdown_note
  id, user_id, local_date, learned, blocked, created_at
```

Two notes.

`plan_block.rollover_count` is denormalized from the item's history on purpose. It is
read on every render and computing it by walking history is the kind of query that is
fine at 100 items and miserable at 10,000.

`day_plan` is versioned by `status` rather than overwritten. Regenerating a plan
supersedes the old one and keeps it. You will want the history the first time you ask
whether the planner is actually any good.

---

## 4. What the brief shows

Ordered. Sections vanish entirely when empty rather than rendering an empty header.

1. **Timezone change**, if today differs from yesterday. First, above everything.
2. **Today's plan** — the proposed blocks, with the protected block marked.
3. **Capacity line** — one sentence: "6h 15m available, 5h 30m planned, 3 items did not
   fit."
4. **Slipping** — due within 48 hours with no progress.
5. **Awaiting others** — owed to you, with age. The section nobody tracks manually and
   the one that justifies the build.
6. **Goal pulse** — active goals, target progress, staleness or risk where flagged.
7. **Rollover** — anything rolling over for the third time, with the drop-or-do question.
8. **Needs review** — low-confidence extractions, accept or reject.

Under 400 words total. Every line links to its source.

---

## 5. Acceptance criteria

- A day with 5 hours of meetings never receives a plan containing more than
  `capacity_minutes` of work.
- A fully-booked day produces the "no deep work available" line rather than an empty plan.
- An item proposed and not completed appears at the top of tomorrow's plan.
- An item rolling over a third time triggers the drop-or-do question exactly once.
- Flying Phoenix to Coimbatore: the next brief leads with the timezone change and the
  working window shifts. No block is scheduled at 03:00 local.
- Monday planning reports committed hours against available hours, and names the gap when
  targets exceed capacity.
- A goal with no checkpoints for 14 days shows serious staleness; a goal ahead of pace
  with no checkpoints for 8 days shows stale but not at risk.
- Breaking a checklist streak produces no copy beyond the count resetting.
- Regenerating a day plan supersedes rather than deletes the prior one.

---

## 6. Deliberately excluded

Each of these is a plausible feature that makes the product worse.

- **Automatic calendar writes.** Propose, never impose.
- **Pomodoro timers, focus modes, app blocking.** Different product.
- **Productivity scores, streaks with emotional weight, gamification.** The system's job
  is to be accurate and quiet.
- **Inferred goals.** Wrong every time.
- **Multi-goal commitment linkage.** Makes progress uninterpretable.
- **Nagging.** No push for a missed shutdown, no guilt copy on a broken streak, no
  "you haven't opened this in 3 days." The brief arrives; that is the entire nag budget.
