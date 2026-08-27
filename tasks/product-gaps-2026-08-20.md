# Four complaints, 2026-08-20

Owner: *"it is planning for things that are obviously done, for example i already moved in
on the 9th and it was resolved, isnt updating schedule everyday and isnt tracking canvas
well enough and my schedule changed and it didnt recognize it and didnt change my calendar
either"*

Every claim below is a query or a file, named. Nothing here is inferred from vibes.

---

## 1. The plan runs at 5pm, not 5:45am — every dated job is firing on IST

`day_plan.generated_at` is `00:15 UTC` day after day. **05:45 IST = 00:15 UTC = 17:15
Phoenix.** The jobs are firing at their scheduled wall-clock time *in India*.

| job | plist says | log mtime |
|---|---|---|
| `com.backglass.plan` | 05:45 | Aug 20 **17:18** |
| `com.backglass.brief` | 06:00 | Aug 20 **17:30** |
| `com.backglass.shutdown` | 22:00 | Aug 20 **09:33** |

Uniform offset across every job ⇒ the stale timezone in `UserEventAgent-Aqua`, which
`tasks/lessons.md` 2026-08-17 already diagnosed: SIP refuses to restart it, `launchctl`
reload does nothing, **only a reboot clears it.**

Consequences, both visible in the data:

- The "morning plan" is built at 5:18pm for a day that is over. `capacity_minutes: 0` on
  plans 51, 52, 50, 46, 43, 41, 39 — there are no hours left to plan into, so everything
  overflows (`overflow_count: 170` today).
- The morning brief has been an **evening** brief for weeks.

So the schedule *is* rebuilt every day. It is rebuilt at the wrong end of the day.

**Second, separate finding:** `accepted_at` is NULL on all 52 `day_plan` rows. No plan has
ever been accepted. Whether that is a UI problem or an intent problem is an open question.

## 2. The schedule changed on ~Aug 10. Both versions are still in the ledger.

`calendar:apple` holds two generations of the class timetable — one fetched Aug 3–5, one
fetched Aug 10 — with different EventKit UUIDs. Both overlap the window Aug 20–26, so this
is a real comparison, not a windowing artifact.

Events that exist **only in the pre-change generation** and are still counted as busy:

| ghost event | when (Phoenix) |
|---|---|
| BIO 181 | Thu 15:00 |
| BIO 181 (Lab) | Fri 08:00 |
| CHM 113 (Lab) | Wed 10:00 |
| CHM 113 (Recitation) | Tue 09:00 |
| LIA 101 | Mon 10:10 |

Added in the new generation: CHM 113 on Mon + Wed, CIS 236 on Wed, HON 171 on Tue, BIO 181
on Wed, PSY 101 on Thu.

Root cause: `source_item` is immutable (docs/03) and the calendar connector has **no way to
say "this event no longer exists upstream."** `apple_calendar.py` re-reads a bounded window
and leans on `content_hash`, so a deleted or moved event is never retracted — it simply
stops being re-emitted while the old row lives forever.

`plan/capacity.py::fixed_events` selects `source LIKE 'calendar%'` with no generation
filter, so those five dead slots still block time.

**Correction, checked after the first draft of this note.** The frozen `calendar:asu`
import is *not* a third copy of the pre-change timetable, as claimed here first. Its 200
rows match the **current** schedule — CIS 236 Mon+Wed, CHM 113 Mon/Wed/Fri, HON 171
Tue+Thu — so the manual import was already right and it is the Aug 3–5 `calendar:apple`
generation that is the outlier. The duplication it does cause is free:
`capacity._span_minutes` merges overlapping intervals before measuring, so an event
present twice is charged once. It is redundant, not harmful.

## 3. "It didn't change my calendar" — it never does, by design

- `backglass/web/actions.py:11` — *"nothing here writes to a calendar, sends mail, or
  resolves anything on the owner's behalf."*
- `backglass/plan/planner.py:4` — *"The planner never writes to the real calendar without
  confirmation."*

Never-write is a deliberate decision, not a defect. It is also plainly not what the owner
expects. **This needs an owner ruling, not a fix.**

## 4. Nothing closes an obligation that time has already answered

Move-in (commitment #8, `Move-in: Willow Hall 502, 8:00am`, due 2026-08-09) is still
`open`, 11 days later. Why, precisely:

- `staleness.STALE_OVERDUE_DAYS = 14`. At 11 days it is not stale yet, so the planner is
  still entitled to schedule it. It becomes stale on Aug 23.
- Even then, stale only **asks**. `logic.py` closes only on *positive contradiction* — text
  that reports the thing already happened — and an event the owner simply attended leaves
  no such sentence anywhere.

Board state: **366 open, 64 of them past due**, oldest `Clean fishtank` due 2026-01-06.
23 questions waiting, and `STALE_BATCH_LIMIT = 5` per refresh means the backlog cannot
drain at the rate the drip offers.

Three distinct defects tangled here:

1. **Event-shaped obligations never expire.** "Attend X on date D" is contradicted by D
   having passed. `logic.py` already closes *"a question about a day that has ended"* —
   this is the same rule shape. Must stay event-shaped only: a **deliverable** past due is
   still owed, and auto-closing those is exactly the silent-data-loss failure the module's
   own docstring warns about.
2. **Canvas items can never self-close.** The ICS feed carries no submission state
   (docs/07 §Canvas), so all 141 assignment commitments read `open` forever, including the
   ones already handed in. They start aging tomorrow.
3. **Re-extraction resurrects resolved work.** Commitment 92 `communicate housing issue to
   ASU University Housing` is `done`; 213 and 304 are the same obligation from the same
   `source_item` (occurred 2026-06-01T18:06:43), both `open`. The owner resolved it and
   extraction re-created it.

---

## What I propose

### Bucket A — owner actions, no code

- [ ] **Reboot the Mac.** Fixes #1 outright: plan back to 05:45, brief back to 06:00.
      Verify per the 2026-08-17 lesson — arm a throwaway job two minutes out and watch for
      its log. The plist agreeing with `launchctl print` proves nothing; it agreed all along.
- [ ] **One-pass board scrub.** Not the 5-a-day question drip — a single screen listing
      every expired / stale / duplicate commitment for confirmation in one sitting.
      Otherwise 64 overdue items never drain.

### Bucket B — code fixes  ·  BUILT 2026-08-20

- [x] **`logic.py`: expire event-shaped obligations whose day has ended.**
      `_events_whose_day_has_passed`, registered in `check`. Attendance verbs only, strictly
      `< today`, dropped rather than resolved because Backglass does not know whether the
      owner attended — only that the hour is gone. Against the live board it disposes of
      exactly one row: commitment 8, move-in. Nothing else among 366 open commitments
      matches, which is the number that matters.
- [x] **`logic.py`: Canvas assignments past a 7-day grace** (`CANVAS_GRACE_DAYS`), per the
      owner's ruling. Bounded by `source = 'canvas:ics'`, never by sentence shape: a mail
      asking for the same essay is still owed. 0 today, 44 by mid-September, 68 by October —
      it ages in rather than firing a wall at once. Delete this rule if `CANVAS_TOKEN` is
      ever granted; the API path carries submission state and supersedes it.
- [x] **Calendar retraction.** Migration 0030 `source_item_retraction`, `backglass/retraction.py`,
      `apple_calendar.retractable_window()`, reconcile hooked into `sync._ingest`, and
      `capacity.fixed_events` now excludes retracted rows. `source_item` is never touched,
      so the history stays readable and a wrong retraction is one `restore()` away.
      The safety property is the important half: a connector that lost any calendar returns
      no window and nothing is retracted, because a timed-out Apple Event returns an empty
      list that looks exactly like a cleared calendar.
- [x] **One-pass board scrub** — `backglass/scrub.py` + `/scrub`. Three groups with the
      reason on every row: gone quiet (46), already handled once (28), past due (13) — **87
      rows** against the five-a-day drip. It detects and never disposes; every button is one
      the dashboard already had. A dashboard alert points at it above `SCRUB_ALERT_FLOOR`.
#### Verified on the live ledger, 2026-08-21 03:28

The scheduled sync fired first with the fixed connector and retracted **16 rows**; the
manual apply that followed retracted **0**, which is the idempotency rule checking itself
in production. All five calendars read completely, none failed.

Monday 2026-08-24, before and after:

```
before: BIO 181 x3, CHM 113 x2, CIS 236 x3, LSB 191 x3, LIA 101, New Event   (13)
after : BIO 181 x2, CHM 113 x2, CIS 236 x2, LSB 191 x2                        (8)
```

LIA 101 is off the day. So are Thursday's BIO 181, Friday's BIO lab, Wednesday's CHM lab,
Tuesday's 16:00 CHM recitation and two stray "New Event" rows. The remaining pairs are one
class present in two calendars; `_span_minutes` merges overlapping intervals, so they cost
nothing.

**The finding under the finding:** the owner's *old* timetable lives in the **Work**
calendar and the current one in **Family**. Every true ghost came from Work; every
twin-survives row from Family. Backglass read both and had no concept of an event ceasing
to exist, so it planned around the union of two schedules. Worth deleting the Work
remnants in Calendar.app too — Backglass ignores them now, but nothing else does.

#### It nearly did the opposite — read this before trusting it

Verified against the live ledger three times, read-only, before anything was believed. The
first run refused to retract, which looked like a bug and was the guard working. Behind it:

1. **A JXA `catch (e) { continue; }` swallowed macOS's -1712 Apple Event timeout.** A
   calendar that timed out returned `[]` with exit code 0 and recorded no failure — a
   timed-out calendar and an empty one were indistinguishable. Removed: the script is
   invoked once per calendar, so the isolation the catch provided was already there, and
   letting the error propagate makes `run_osascript` raise into `failed_calendars`, which
   is what `retractable_window` already refuses to certify on.
2. **`seen_ids` recorded what was *emitted*, not what the store *returned*.** The connector
   deliberately suppresses a duplicate event across calendars, and HON 171, PSY 101 and
   CIS 236 each sit in two of the owner's calendars — so every suppressed twin read as
   deleted. It now records every uid the store returns, before dedup and before `_to_item`
   drops all-day banners and cancellations.

Stacked, those two put the reconciler one clean run away from retracting **sixteen live
classes**: one calendar returned 53 events, another silently returned none, and the read
certified. All 32 calendar and retraction tests passed on both defects, which is the point
— the would-delete list had to be printed and classified row by row before it was trusted.

- [ ] **Fix duplicate re-extraction** (92 vs 213/304). Not built. The scrub board now
      *surfaces* these 28 rows, so they are clearable, but nothing stops extraction making
      more. `actions.drop` tombstones and `resolve` does not, which is the likely mechanism.
- [ ] **Retire `calendar:asu`.** Not selected. Re-checked and it is **not** the problem it
      looked like: the 200 manual rows match the *current* timetable, not the pre-change one,
      and `capacity._span_minutes` merges overlapping intervals, so the duplication costs
      nothing. It is redundant, not harmful.

### Bucket B — original list

- [ ] **`logic.py`: expire event-shaped obligations whose day has ended.** Provenanced
      (`resolution_note` starting `logic:`), tombstoned not deleted, `decision` row written
      — same contract as every other rule in the module. Event-shaped only.
- [ ] **Calendar generation handling.** Retract events that vanished upstream, so capacity
      stops blocking the five ghost slots. Options: a tombstone/prune path through the
      migration-0005 delete gate (`imessage.prune` is the precedent), or filter capacity to
      the newest generation per calendar. Check first whether `_fixed_events` merges
      overlapping intervals before claiming the duplicates also cost capacity.
- [ ] **Retire `calendar:asu`.** 200 rows, no connector, frozen since 2026-07-31,
      superseded by `calendar:apple`, and currently a third copy of the old timetable.
- [ ] **Fix duplicate re-extraction** (92 vs 213/304). Determine whether the current dedupe
      — the sync reported `deduped 16` — already covers this or only new cases.

### Bucket C — rulings I need from the owner, not decisions I should make

- [ ] **Should Backglass write to Calendar.app?** Currently never, deliberately. Options:
      keep read-only / write only on explicit confirm / write the accepted plan through.
- [ ] **How aggressive on Canvas past-due?** Auto-expire N days after the due date, or
      batch-ask, or a one-tap "done" on the dashboard. 141 items make this urgent.
- [ ] **Rebuild the desktop app?** `state` reports `stale_python:
      ['backglass/connectors/canvas_ics.py']` — the app froze the old connector at build
      time. Scheduled sync runs from the checkout, so the pipeline is already fixed.

---

**Standing warning:** tomorrow's plan and brief will be flooded — 141 new Canvas
commitments on a board whose overflow is already 170. Buckets A and B are what make that
survivable.
