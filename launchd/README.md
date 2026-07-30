# Scheduling

docs/10 §Scheduling: **launchd on macOS, one plist per job, each invoking the CLI.**

    "Do not use an in-process scheduler (APScheduler, `schedule`, a `while True` loop). It
    dies silently with the process and you find out three days later when you notice the
    brief stopped. The whole point of the Sources panel is catching that failure, and an
    in-process scheduler makes the failure invisible to it."

That is why there is no `--daemon` flag anywhere in this codebase and why these are files
rather than code.

## Install

Edit the paths in each plist (they need absolute paths — launchd has no shell and no
`PATH` worth relying on), then:

    cp launchd/*.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.cognifer.backglass.*.plist

Check they are registered:

    launchctl list | grep backglass

## Jobs

| plist | cadence | command |
|---|---|---|
| `com.cognifer.backglass.sync.plist` | every 30 min | `backglass sync` |
| `com.cognifer.backglass.plan.plist` | 05:45 local | `backglass plan` |
| `com.cognifer.backglass.brief.plist` | 06:00 local | `backglass brief --send` |
| `com.cognifer.backglass.shutdown.plist` | 18:00 local | `backglass shutdown` |

The cadences come from docs/02 §Scheduling. `plan` runs fifteen minutes before `brief` so
the brief has a plan to report.

## When the laptop sleeps through 06:00

docs/10: "If the laptop sleeps through 06:00 regularly, move the jobs to GitHub Actions on
a schedule with the SQLite file in a private repo or on object storage."

`StartCalendarInterval` fires on wake if the machine was asleep at the scheduled time, so
a closed lid usually produces a late brief rather than no brief. A *shut down* machine
produces neither, and that is the signal to move.

Note that the timezone follows the machine, not `TZ_RANGES` — launchd fires at 06:00
wherever the laptop thinks it is. The brief itself resolves the active zone correctly
(docs/04 P13), so a stale plist means a brief that arrives at the wrong hour, never one
that is wrong about its contents.
