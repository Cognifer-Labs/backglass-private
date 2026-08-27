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
                    plus confirmed engagements with a stated hour,
                    plus the configured routines that fall inside the window (§1.9)
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
  decision 15, meeting-prep 30, message 15, call 20, form 30, errand 30, log 10,
  unknown 45.
- The owner can override on any item, and an override is sticky for that item.
- Track actuals. After 30 completed items, report the ratio of estimated to actual in the
  weekly review, and let the owner adjust the type defaults. Do not auto-adjust silently.

The last five types were added on 2026-08-15 after measuring the ledger rather than
reasoning about it: 273 of 287 open commitments were classifying as `unknown`, so 256 of
them carried an identical 45-minute estimate and every capacity number the planner
printed was arithmetic over a figure nobody had chosen. The leading verbs of that
unclassified set were *send, complete, submit, pick, bring, ask, apply, accept, confirm,
get, call, follow, notify, reply, email* — a student's ledger of forms, applications and
short messages, which the original four types describe none of.

Applying the table is not the auto-adjustment this section forbids. A `type_default` is a
pure function of the commitment's text and this table, so `estimates.backfill` re-derives
those rows on every planner run and a change here reaches the whole open backlog. What
stays forbidden is moving the *numbers* from measured actuals; `ratio_report` reports the
ratio and writes nothing.

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
5. Everything else fills by priority, then by rollover (P10, inside the band), then by
   the owner's stated lanes, then by age.

Two numbers bound what may be selected at all, and they are different numbers: capacity
is the day's free minutes **summed**, and a block needs them **contiguous**. The planner
never selects a sitting longer than the day's largest free run — a 90-minute item cannot
be placed in a day of 60-minute fragments, and selecting it anyway spends the budget and
schedules nothing (added 2026-08-24; it was costing 175 of 183 candidates on a day with
three and a half free hours).

Coursework — an assignment with an estimate read off its own text — has first claim on
`homework_target_minutes` of the day (default 120, owner's ruling 2026-08-24). It is a
reservation, not a target: it caps at what the day's coursework actually wants, and it
lapses entirely when there is none, so a day with nothing due is never held empty.

Priority is derived, not entered: `overdue > due today > advances an at-risk goal > due
this week > everything else`. The owner can pin an item to a specific slot, and a pin
always wins.

### 1.5a The conflict priority list

Owner's ruling, 2026-08-24. Dates decide *when*; this decides *what* when two things want
the same slot. It lives in `backglass/plan/priority.py`, is shown on `/schedule`, and is
overridable by the `preferences/planner.priority` fact.

| # | tier | how it wins |
|---|---|---|
| 1 | fixed class, lab and exam | carved out of the window before anything is planned |
| 2 | exam or assignment due within 48h | ranked first among commitments |
| 3 | gym, meals, sleep | placed as routines before any work |
| 4 | homework due this week | ranked above everything below, and holds the day's reservation |
| 5 | coding / OrgTruth | a standing block, placed after the real work |
| 6 | clinical and premed admin | above errands, below coursework |
| 7 | errands, email, social | everything the list does not name |

Three of the seven were already true by construction and are written down so the list is
complete rather than only the part that needed code. The half that did need code is tiers
2, 4, 6 and 7 — the ordering among commitments — which reaches the planner as the lane
rank `planner.order` has taken since August and been handed an empty set ever since,
because the fact it reads had never been written.

**Tier 2 sits above tier 3 and nothing acts on that.** The list says a paper due tomorrow
outranks the gym; the planner never moves a meal or a workout to make it true. A routine
is the owner's own decision about their day, and a planner that quietly deleted dinner to
fit an essay is the surface nobody trusts twice. What happens instead is that the plan
names the collision — the lower-tier blocks holding the time, in tier order, cheapest to
give up first — and the owner moves one.

### 1.6 Rollover

At the end of the working window, anything proposed but not marked done becomes
**rollover**.

| ID | Requirement |
|----|-------------|
| P10 | Rollover items appear at the top of the next day's proposal, above newly selected work — **within their priority band**, not above it. Clarified 2026-08-24 after the code read it as an absolute first key: anything that had ever rolled then beat everything that had not, whatever either was due, permanently. On the owner's real 2026-08-26 that gave the day's first ninety minutes to an assignment due 13 November while seven due 28 August waited behind it. §1.5 rule 2 is the higher rule and always was; P10 orders the things that rule leaves tied. |
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

### 1.9 Routines, relaxation, and study

*Owner's ruling, 2026-08-21: "everything should be planned around my schedule and fixed
events; breakfast, lunch, dinner and gym should be planned around this, along with some
amount of relaxation time and study time and homework time."*

A day is not a working window with meetings punched out of it. It is a life with a
working window inside it, and the parts that are not work — eating, the gym, the shower
after it, the evening — are the parts a planner is most tempted to treat as empty. They
are configured as **routines** (`ROUTINES`, parsed by `config.parse_routines`), and they
sit on the day exactly as a meeting does.

**The hour in a routine is a preference, not a decree.** This is the whole of §1.9 and
it was learned the plain way: the live plan for Monday 2026-08-24 read

```
12:20pm–1:10pm  CHM 113 [fixed]
12:30pm–1:15pm  Lunch [routine]
```

which is not a scheduling conflict so much as a plan that is wrong about when the owner
eats. Nothing in the capacity arithmetic was wrong — overlapping spans are merged, so
the minute was charged once — and that is what makes it the dangerous shape: a plan that
is internally consistent and describes a day nobody lived.

So a routine whose preferred span lands inside a class, a meeting or a confirmed
engagement **moves to the nearest free gap**, searching outward from the preferred
start, the earlier of two equally distant gaps winning. Eating before the class beats
eating after it, and either beats a coin toss — the tie is broken deterministically
because 0028's `inputs_fingerprint` hashes the placement, and a routine that lands on a
different minute on two runs over one unchanged day reports drift that is only
arithmetic, superseding a live plan every sync.

**The drift is bounded** (`capacity.ROUTINE_MAX_SHIFT_MINUTES`, two hours). Unbounded, a
fully-booked afternoon puts lunch at four o'clock, and a meal moved that far is not the
meal that was asked for: it is the planner rewriting the day and still calling it lunch.
Past the bound the routine keeps its hour and states what it collided with, which is a
fact the owner can act on — by moving something, or by eating anyway and knowing the
plan knows.

**A routine that is an obligation rather than a habit is pinned** with a trailing `!`
(`banner@16:00+240!@wed`). Wednesday volunteering at a stated hour is an appointment
somebody else made; a planner that quietly moved it to five o'clock would be inventing
one. Pinned routines are placed first, and every placed routine joins the obstacle set,
so breakfast cannot be shifted onto lunch.

**The evening is protected by the working window, not by new machinery.** "Relaxation
from nine, and no work after it" is exactly a window that ends at 21:00 with
`relax@21:00+120` outside it: the block renders on the schedule, spends no capacity, and
there is nothing left for the planner to fill. A separate work-hours ceiling would be a
second concept that means the same thing as the first and can disagree with it.

**Homework is what the planner already does; study is what a day without it should still
hold; coding is what the owner asked to be there every day.** The third is the same
mechanism as the second with the condition removed — see `Settings.coding_block_minutes`
for why that is two settings rather than a config grammar for one boolean. A coursework commitment carries an estimate read off the assignment (§1.3,
`estimate_source = 'analyzed'`) and is scheduled by name. A day where none of that was
selected gets one `study` block instead — a block, not a commitment: nothing is written
to the ledger, nothing rolls over, and there is nothing to mark done but the block
itself. A plan may say "read" without inventing an obligation the owner never made, and
`rollover.open_blocks` leaves the kind out for that reason: an hour of reading nobody
did is not a debt, and rolled it would arrive tomorrow as work, then eventually trigger
P11's drop-or-do question about a commitment that does not exist.

| ID | Requirement |
|----|-------------|
| P17 | A routine's configured time is preferred, not fixed. A routine overlapping a fixed event moves to the nearest free gap that fits it — earlier wins ties — bounded by `ROUTINE_MAX_SHIFT_MINUTES`. |
| P18 | A routine that cannot be placed within that bound keeps its preferred hour and the plan states what it overlaps. Never silently dropped, never silently moved out of the day. |
| P19 | A routine marked pinned (`!`) never moves, is placed before flexible ones, and reports no conflict — a pin is a decision, not a collision. |
| P20 | A day whose plan holds no coursework gets one study block, capacity-permitting. It is never rolled over and never becomes a commitment. |
| P21 | Every day gets one coding block, capacity-permitting (owner's ruling, 2026-08-24). Same mechanism as P20 and unconditional: the owner's own building produces no commitment for the board to schedule, so without a standing block it is the one lane that never appears on a day at all. |
| P22 | A standing block (P20, P21) is sized against the largest **remaining** free run, not the capacity left over. Those are different numbers, and a block asking for a hole the day does not have is not placed — and a standing block that fails to place says nothing, so it simply vanishes. Shrunk to fit, it lands. |

Placement happens in `capacity.day_events` — the one builder the capacity model, the
persisted plan and the schedule page all read — so the three cannot disagree about when
lunch is.

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

Two kinds were added later and are deliberately not weekly:

| kind | example | how it completes |
|---|---|---|
| **total** | "200 research hours" | lifetime `SUM(delta)`, no week clamp (Phase 10) |
| **periodic** | "see your academic advisor, every 182 days" | a checkpoint resets the clock (migration 0024) |

`periodic` exists for the one class of obligation the ledger structurally cannot see.
Every other record in this system arrives from somewhere — an email carries a deadline,
Canvas carries an assignment, a calendar carries a meeting. Nobody sends mail to say six
months have passed since your last advising appointment, that the internship cycle has
reopened, or that it is time to find a research placement. Those are reached by a clock
or by nobody, and the lead times are long enough that missing one costs a year.

It holds a cadence in days (`target.every_days`) — the same primitive as
`entity.touch_every_days`, and one unit rather than two arithmetics. **Due** at the
cadence, **overdue** at twice it, which is the ruling `people/touch.py` already made for
the same question about people: one number for the owner to choose rather than a
warn/cold pair. The clock is anchored on the newest checkpoint, falling back to the
target's `created_at`, so a target written today is not instantly overdue.

G2–G7 do not apply to it. It does not reset weekly, it is never missed by a week, and it
contributes zero to the §2.3 capacity check — a six-month obligation amortises to about
two minutes a week, and a capacity gap that includes it is a gap nobody can act on. The
daily brief raises it when it comes due, with the day count; the Monday brief skips it,
for the same reason it skips totals and milestones.

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
  id, goal_id, kind(cadence|milestone|maintenance|total|periodic), title,
  weekly_count, estimated_minutes_each, total_count, every_days,
  active, created_at

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
2. **Today's plan** — the proposed blocks, with the protected block marked, and any
   routine the day left no room for (P18). The conflict is recomputed from the day's
   events rather than stored: the CLI's proposal notes are in-memory, and a fact that
   never reaches the brief reaches nobody.
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
- An item proposed and not completed appears at the top of tomorrow's plan, ahead of the
  work it is tied with. It does not jump a band: something due tomorrow is done tomorrow,
  and a rolled item with no due date at all does not outrank it (corrected 2026-08-24 —
  see P10).
- An item rolling over a third time triggers the drop-or-do question exactly once.
- Flying Phoenix to Coimbatore: the next brief leads with the timezone change and the
  working window shifts. No block is scheduled at 03:00 local.
- Monday planning reports committed hours against available hours, and names the gap when
  targets exceed capacity.
- A goal with no checkpoints for 14 days shows serious staleness; a goal ahead of pace
  with no checkpoints for 8 days shows stale but not at risk.
- Breaking a checklist streak produces no copy beyond the count resetting.
- Regenerating a day plan supersedes rather than deletes the prior one.
- A lunch routine at 12:30 against a class from 12:20 to 13:10 is planned at 13:10, and
  planning the same unchanged day twice places it on the same minute both times.
- A day with no gap wide enough for a meal still shows the meal, at its preferred hour,
  with the sentence naming what it overlaps.
- A day whose plan holds no coursework holds a study block; a day holding coursework
  does not, and no study block is ever rolled into tomorrow.

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
