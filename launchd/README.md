# Scheduling

docs/10 §Scheduling: **launchd on macOS, one plist per job, each invoking the CLI.**

    "Do not use an in-process scheduler (APScheduler, `schedule`, a `while True` loop). It
    dies silently with the process and you find out three days later when you notice the
    brief stopped. The whole point of the Sources panel is catching that failure, and an
    in-process scheduler makes the failure invisible to it."

That is why there is no `--daemon` flag anywhere in this codebase and why these are files
rather than code.

## Install

The plists live as templates under `launchd/templates/*.plist.tmpl` — they are not
directly installable files, because the absolute repo path and `uv` binary path are
different on every machine.

    backglass schedule install

Renders every template for this machine (repo path via the same resolution
`backglass/config.py` uses, `uv` via `shutil.which`), writes the result to
`~/Library/LaunchAgents/`, and runs `launchctl load` on each. Use
`backglass schedule install --dry-run` to see the rendered plists without writing or
loading anything — useful after moving the repo or reinstalling `uv`, before trusting
the real run.

**Manual fallback**, if you'd rather not use the CLI or need to tweak a job by hand:

    cp launchd/templates/*.plist.tmpl ~/Library/LaunchAgents/
    # then replace {{REPO_DIR}}, {{UV_BIN}}, {{HOME}} in each with real absolute paths,
    # rename to strip the .tmpl suffix, and:
    launchctl load ~/Library/LaunchAgents/com.backglass.*.plist

Check they are registered:

    launchctl list | grep backglass

## Jobs

| template | cadence | command |
|---|---|---|
| `com.backglass.sync.plist.tmpl` | every 30 min | `backglass sync` |
| `com.backglass.plan.plist.tmpl` | 05:45 local | `backglass plan` |
| `com.backglass.brief.plist.tmpl` | 06:00 local | `backglass brief --send` |
| `com.backglass.shutdown.plist.tmpl` | 18:00 local | `backglass shutdown` |
| `com.backglass.batch-submit.plist.tmpl` | 22:00 local — **optional, batch mode only** | `backglass batch submit` |
| `com.backglass.backup.plist.tmpl` | 02:00 local | `backglass backup` |
| `com.backglass.batch-collect.plist.tmpl` | 05:30 local — **optional, batch mode only** | `backglass batch collect` |

The cadences come from docs/02 §Scheduling. `plan` runs fifteen minutes before `brief` so
the brief has a plan to report.

`backup` snapshots the ledger to `~/Library/Application Support/Backglass/backups`
(override with `BACKUP_DIR`) and rotates to seven dailies plus four weeklies. It runs at
02:00, after the day's writes and before the morning jobs. The database is the only copy
of the record and docs/08 keeps `data/` out of every cloud sync, so `doctor` treats this
job as required and fails if the newest snapshot is more than 48 hours old.

Batch mode (docs/02 §Cost control) trades latency for a 50% discount on extraction:
`submit` runs ingest/rules/triage synchronously at 22:00 and hands the surviving items
to the Message Batches API; `collect` applies the results at 05:30, before `plan` and
`brief`. Both need an Anthropic API key (`MODEL_API_KEY` or `ANTHROPIC_API_KEY`) and
pair best with `MODEL_BACKEND=anthropic`. Skip both plists to stay fully synchronous —
`doctor` does not require them.

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
