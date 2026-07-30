# Architecture

## Five stages

```
  SOURCES            INGEST           EXTRACT            STORE          SURFACE
 ┌─────────┐       ┌─────────┐      ┌──────────┐     ┌─────────┐   ┌─────────────┐
 │ Gmail   ├──────▶│         │      │  triage  │     │         │   │ morning     │
 │ Drive   ├──────▶│ poller  ├─────▶│  (cheap) ├────▶│ ledger  ├──▶│ brief       │
 │ Notes   ├──────▶│ + dedup │      │    ↓     │     │ SQLite  │   ├─────────────┤
 │ Calendar├──────▶│         │      │ extract  │     │         │   │ dashboard   │
 │ Canvas  ├──────▶│         │      │(careful) │     │         │   ├─────────────┤
 └─────────┘       └─────────┘      └──────────┘     └─────────┘   │ day planner │
                                                          ▲        └─────────────┘
                                                     ┌────┴────┐
                                                     │  goals  │  entered by hand
                                                     └─────────┘
```

Each stage is independently testable. The seam that matters most is between extract and
store: extraction is nondeterministic and expensive, the store is neither.

## Why no vector database

The queries this system answers are fixed and known at build time:

- What is due today?
- What is overdue?
- What is owed to me, and for how long?
- What advanced this goal this week?
- What is new since yesterday?

Every one of those is a `WHERE` clause over typed columns. Semantic search exists to
handle queries you could not anticipate. Here there are none, so paying embedding cost
at ingest and similarity cost at read buys nothing and adds a failure mode where the
brief silently omits something because it ranked poorly.

If a chat interface is ever added, revisit this. Until then, do not build it.

## Immutable source items

Raw items are written once and never modified. Extraction reads them and writes derived
records elsewhere.

This exists so extraction can be re-run against a better prompt without re-fetching, and
so a bad prompt version is recoverable. You will re-run extraction many times, and every
time you will be glad the raw text is still there.

`source_item.extraction_version` records which prompt version last processed the item.
Bumping the version and re-running produces a diff you can inspect before accepting.

## Two-tier extraction

Running every email through a careful extraction prompt is the fastest way to make this
too expensive to keep running. Inboxes are mostly newsletters, receipts, and
notifications.

### Tier 1: triage

Rules first, and rules handle most of it:

- Sender on a known-noise list
- `List-Unsubscribe` or other bulk headers present
- `no-reply@` style sender
- Calendar invite (handled by the calendar connector, not extraction)
- Automated notification patterns

Whatever the rules cannot classify goes to a small fast model with one binary question:
does this contain a commitment, a decision, a deadline, or a fact worth remembering?

Record the verdict on the source item so triage is auditable and tunable.

### Tier 2: extraction

Survivors go to a careful prompt returning strict JSON against a schema. This is where
money is spent and it is worth spending.

**Expect tier 1 to eliminate 90 to 95 percent of volume.** If it does not, the rules are
too permissive and the cost model breaks. Measure this and put it in the Sources panel.

## Cost control

```
monthly_cap        configured, hard
on_cap_reached     degrade to triage-only, log loudly, surface in Sources panel
per_item_ceiling   skip and park anything whose input exceeds it
```

The cap is enforced in code, not monitored. A pipeline that can silently 10x its spend is
a pipeline you turn off.

## Scheduling

| job | cadence |
|---|---|
| ingest | every 30 min |
| extract | queue-driven, as items arrive |
| day plan | 05:45 local |
| brief | 06:00 local |
| evening shutdown prompt | end of working window |
| weekly Monday planning | replaces Monday brief |
| weekly Friday retro | appended to Friday brief |

Local cron or launchd if a machine is always on; GitHub Actions on a schedule otherwise,
which is free and needs no hardware.

Nothing here is real time. An hour of lag is acceptable everywhere.

## Failure policy

- A failing source degrades and does not block the others.
- Failures surface in the dashboard Sources panel and in the brief.
- The process exits non-zero at the end so a scheduled run reports failure.
- Auth expiry is treated as a first-class visible state, not an exception in a log.

The Sources panel looks like ops chrome and is essential. When the brief goes quiet
because Gmail auth expired three days ago, it is the only place that would tell you.

## Stack

Python 3.11+, SQLite, FastAPI for the one dashboard page, server-rendered HTML, plain CSS
custom properties from `design/tokens.css`.

No frontend framework, no ORM heavier than raw SQL with a thin helper, no message queue.
The whole system should be readable in an afternoon a year from now.
