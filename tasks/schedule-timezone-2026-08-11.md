# The launchd schedule has been firing 12½ hours off since 24 July

Every `StartCalendarInterval` job on this machine fires on India time. The sync is the
only one that has been right, because it counts seconds and seconds are the same
everywhere.

| Job | Plist says | Actually fires (MST) |
|---|---|---|
| plan | 05:45 | 17:15 |
| brief | 06:00 | 17:30 |
| shutdown | 22:00 | 09:30 |
| backup | 02:00 | 13:30 |
| sync | every 1800s | on time |

Every one is the scheduled hour minus 12:30 — exactly the offset between Asia/Kolkata
(UTC+5:30) and America/Phoenix (UTC−7).

## What it cost

- **The morning brief is written at 17:30.** It has been generated every day since
  4 August and describes a day that is over by the time it is made.
- **The day planner runs at 17:15**, planning a day that has already happened. That is
  why the last two day plans have `capacity_minutes = 0` and a hundred items in
  overflow, and why `plan.log` says "Fully booked — 0m of capacity". Nothing is wrong
  with the planner: at 5:15pm there is genuinely no day left to plan.
- **The evening shutdown pass runs at 09:30**, asking what got done before the day has
  started. Hence "0 done, 2 rolled" every morning.
- The backup runs at 13:30. Harmless, but not what the file says.

## Root cause, and it is not this program

`launchd` does not evaluate calendar intervals itself — `com.apple.UserEventAgent (Aqua)`
does, and it reads the timezone when it starts and never again.

```
boot / UserEventAgent (Aqua) started   Fri Jul 10 21:09
/etc/localtime → America/Phoenix       Jul 24 18:25     ← 14 days later
uptime                                 32 days
```

The Mac was in the +5:30 zone when that agent started, moved to Phoenix on 24 July, and
has not been restarted since. The agent still believes it is in the old zone. The plists
are correct, `launchctl print` reports the correct hours, and both are irrelevant.

Proved rather than reasoned about, with three throwaway jobs:

- a job labelled **22:52**, the true wall time, loaded fresh — **did not fire**;
- a job labelled **11:27**, chosen because 11:27 − 12:30 = 22:57 — **fired at 22:57:05**;
- so reloading is not the remedy: a job loaded seconds ago gets the stale zone too.

`launchctl kickstart -k gui/$UID/com.apple.UserEventAgent-Aqua` is refused: *"Operation
not permitted while System Integrity Protection is engaged."*

## The fix

**Restart the Mac** (or log out and back in). That is the whole remedy, and it is the
owner's to do — nothing in this repo can restart a SIP-protected system agent.

### Owner's ruling, 2026-08-11: restart after moving; do not redesign

The alternative was to stop using calendar intervals altogether — fire the jobs on a
`StartInterval` tick and let the program decide whether its local time has come and
whether today's work is already done, which is timezone-immune by construction and is
why the sync never broke. It was declined: the owner rarely travels to the +5:30 zone,
so the redesign would be carrying permanent machinery for a rare event.

**So the standing rule is: restart the Mac after changing its timezone.** The `state`
schedule section below is the backstop that makes a forgotten restart visible instead of
silent, which is what made this cost eighteen days the first time. Do not re-open the
interval redesign without new evidence — a second occurrence that the state check failed
to surface would be that evidence.

## What was fixed in code

`backglass state` now has a `schedule` section, because the thing that made this
expensive is that nobody could see it: the plist said one time, the job ran at another,
and each looked right read alone. It reports every job, when it was last actually seen
to run (the mtime of its log — launchd keeps no accessible record of a last fire), and
the distance between the two. Tolerance is 45 minutes, since launchd legitimately defers
jobs missed while asleep, and drift is measured around the clock rather than across it.

Today it reports:

```
drifting: ['com.backglass.backup fires 02:00, last ran 13:38',
           'com.backglass.brief fires 06:00, last ran 17:38',
           'com.backglass.plan fires 05:45, last ran 17:17',
           'com.backglass.shutdown fires 22:00, last ran 09:30']
```

**After a restart these clear one at a time, as each job fires once at its right hour** —
backup at 02:00, plan at 05:45, brief at 06:00, shutdown at 22:00. A `drifting` list that
is still full at 06:30 tomorrow means the restart did not take.

## Separate, and the owner's call

- **The brief is deliberately not emailed** (owner's ruling, 2026-08-11). `BRIEF_TO` is
  unset, so `brief --send` saves it and says so, which docs/05 already treats as a
  non-error. The owner reads it at `/brief` and does not want delivery wired. Eight
  briefs generated since 4 August are all readable there. Do not offer this again.
- The OAuth failures in `sync.err` are historical; the last five runs are clean.
- `sync.err` also records a date read as 2027: `'2026-05-14T19:30' resolved to
  2026-05-14, before the message date 2026-05-15; read as 2027-05-14`. Same shape as the
  quick-add rollforward in `tasks/dead-buttons-2026-08-11.md` — an explicit date being
  pushed a year forward. Not investigated.
