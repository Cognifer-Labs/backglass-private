# The autonomous day organizer — what it still cannot do

Derived 2026-08-10 by running the real planner against the real ledger, including the
semester that starts on the 20th. Every claim names the command behind it.

**The boundary first, so nothing here relitigates it.** docs/04 §6 rules out automatic
calendar writes: "propose, never impose." Autonomous here means *the proposal is right
without being corrected*, not that the system starts moving the owner's day. Everything
below is a way the proposal is currently wrong, or a moment it has no answer for.

---

## 1. Capacity double-counts overlapping fixed events — and the semester is full of them

`capacity.fixed_minutes` sums each event's in-window span. It does not union them, so an
hour covered by two events is subtracted twice. The owner's class schedule overlaps
constantly — three-way on Wednesdays.

```
2026-08-24 (Mon)  reported 415m fixed   true union 340m   over-subtracted  75m
                  capacity 210m         should be 285m

2026-08-26 (Wed)  reported 545m fixed   true union 415m   over-subtracted 130m
                  capacity  80m         should be 210m
```

Wednesday is the shape of the problem: the planner believes the owner has 80 minutes and
will propose a nearly empty day, four days into the semester. P3's "if capacity is under
60 minutes, do not propose a plan" is one bad Wednesday away from firing on a day with
three and a half free hours in it.

- **Implement**: merge fixed spans before subtracting. `web/routes/schedule._place`'s
  cluster logic is the same shape and can be borrowed rather than reinvented.
- **Test**: two overlapping fixed events subtract their union, not their sum; a
  three-way overlap likewise; back-to-back events (touching, not overlapping) are
  unchanged.
- **Priority**: highest on this list. It is wrong now, wrong by more every Wednesday, and
  the error is silent — a smaller day looks like a busy day, not like a bug.

## 2. The planner has no notion of "now", so a replan proposes hours that are gone

`planner.propose(conn, settings, day)` takes a date. There is no current time anywhere in
it, so regenerating today's plan at 12:33 proposes:

```
10:00–11:30  email signed waivers to Nyasha Stone Sheppard
11:30–12:15  Call the phone-only hospices closest to ASU
             … 2 of 13 work blocks end before the current wall clock
```

This is not hypothetical. `com.backglass.plan-catchup.plist` carries `RunAtLoad=true`, so
opening the laptop at 3pm on a day with no plan produces one whose first third already
happened. The owner then has to mentally discard the top of their own day, which is the
work the planner exists to do.

- **Implement**: `propose` takes an optional `now`; on the current day, selection starts
  at the next free minute rather than the window start. Past fixed events still subtract —
  the morning was spent — but no *work* is placed behind the clock.
- **Test**: a plan generated at 14:00 places no work block before 14:00; the same call for
  tomorrow is unchanged; a plan generated before the window opens behaves as it does today.
- **Depends on**: nothing. Bounded and local.

## 3. Untitled calendar events silently eat the day

Monday 2026-08-24 carries `New Event` 09:00–10:00 — the placeholder Calendar.app writes
when something is created and never named. It subtracts an hour like any other fixed
event, and on the schedule page it reads as a real obligation.

- **Implement**: treat an untitled or placeholder-titled event as suspect. It should not
  silently spend capacity; the Sources panel or the review queue is where it belongs, since
  a guess about an hour is the same class of thing as a guess about a promise (rule 2).
- **Test**: an event titled "New Event" does not reduce capacity and is surfaced; a real
  event with a blank title in one connector but a real one in another still counts once.
- **Open question for the owner**: is `New Event` on 2026-08-24 real? The fix should not
  assume.

## 4. Two classes are scheduled on top of each other

Not a code defect — a fact the ledger is reporting and nothing is asking about:

```
2026-08-24  LIA 101  10:10–11:00
            BIO 181  10:30–11:45
```

A student cannot attend both. Either the schedule data is stale, one is a section the
owner dropped, or there is a real registration conflict to resolve before the 20th.

- **Implement**: a conflict check over fixed events, surfaced once per new conflict rather
  than every morning. Two things the owner must be at, at the same time, is the single most
  actionable thing a day organizer can say — and it currently says nothing.
- **Test**: two overlapping `fixed` events raise a conflict; a fixed event overlapping a
  routine does not (life bends, classes do not); the same conflict is not re-raised daily.

## 5. Travel between buildings is invisible

`travel_minutes` is 0 on every class day measured. docs/04 §1.2 defines travel as "any
calendar event tagged travel", so it only counts when something else tags it. Back-to-back
classes in different buildings — CHM 113 at 12:20 after BIO 181 ending 11:45 — get the
generic 10-minute buffer and nothing else.

The class rows carry `location`, so the information is present and unused.

- **Implement**: when consecutive fixed events name different locations, extend the buffer.
  A flat campus-crossing constant is honest and needs no map; anything cleverer needs a
  distance source and should wait until the flat version is proven insufficient.
- **Test**: two events at the same location keep the standard buffer; different locations
  get the travel one; a missing location falls back to standard rather than guessing.
- **Priority**: after 1–4. It is a refinement; those are corrections.

## 6. Nothing notices when the day stops matching the plan

The plan is generated at 05:45 and never revisited. If a 90-minute block overruns by an
hour, the remaining blocks are wrong for the rest of the day and nothing says so. The
evening pass asks what happened after the fact; there is no point at which the system
notices the day has diverged while it can still help.

This is the one item that edges toward docs/04 §6's excluded list, and the line is worth
stating: noticing is not nagging. A dashboard that shows the plan re-fitted to the time
that is actually left is a report, not a push notification.

- **Implement**: the schedule page re-fits the remaining blocks against the current clock
  on load — same data, same ordering, no writes, no alerts. Item 2's `now` parameter is
  the whole mechanism.
- **Test**: the page at 14:00 shows the same blocks the 05:45 plan had, re-fitted; the
  stored plan is unchanged; nothing is sent anywhere.
- **Do not**: notify, interrupt, or write to the calendar. §6.

---

## Order

1, 2 and 4 before 2026-08-20 — those are the ones the semester makes wrong or dangerous.
3 with them if `New Event` turns out to be junk. 5 and 6 after, once a real class week has
run and there is evidence about how the days actually fail.
