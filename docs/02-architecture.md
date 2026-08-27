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

**Revisited 2026-08-10, owner's ruling: retrieval is permitted, and the reasoning above
still holds for everything it says.**

The five queries listed are still `WHERE` clauses and must stay that way. What the list
omits is the drop folder, which arrived after this was written: a signed contract, a
scanned letter, a set of meeting notes. "Which letter mentioned the deposit deadline" is
not a typed column and never becomes one, because no schema anticipates every question
about a document that has not been received yet.

So the failure mode this section names — "the brief silently omits something because it
ranked poorly" — is prevented by rule rather than by absence:

| | Ranked retrieval | Typed records |
|---|---|---|
| Brief, planner, dashboard, goals | never | always |
| Finding a document by what it was about | yes | no |

Retrieval is additive. Nothing that decides, schedules or reports may read a similarity
score, and if the index were deleted every existing surface must still be correct. That
is the whole of the concession: the ledger did not become a search tool, it gained a way
to reach the evidence underneath it.

Embedding cost, the section's other objection, is answered by `openai_compatible` — a
local model indexes the drop folder for nothing, and nothing leaves the machine.

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


### What tier 0 costs nothing to drop

Tier 0 was written for mail — bulk headers, `Precedence`, `Auto-Submitted` — and saw only
headers, never the body. On a message store that meant nothing it could catch: measured
over 3,687 real iMessage items, **every one of the 3,167 drops was a model verdict**, so
the pipeline paid a call to be told that an emoji has no substance.

An item with no letters or digits is now dropped before any model sees it: 401 items,
10.9% of that volume, and zero disagreement with the model's own verdicts on the same set.

It is deliberately *not* "drop short messages", and the difference was measured rather
than assumed. A candidate pleasantry list ("ok", "yes", "bet", …) tested against the same
3,687 killed 41 the model had **kept** — those are confirmations, and a bare "Yes"
answering "dinner Friday?" is precisely what the engagement extractor exists to catch.
Length is not a proxy for meaning in a conversation.

### Batches are packed by size, not by count

The constraint on a batch is the context it has to fit into, so items are packed to a
character budget (`triage_batch_size` × the excerpt ceiling) rather than counted out
twelve at a time. A mail runs to the 500-character ceiling; an iMessage averages
twenty-five. Fixed counting made the owner's messages cost 308 calls where **55** do —
an 82% reduction with the same verdicts.

A per-batch item ceiling caps how dense a run of one-word texts can get. Over-packing
degrades safely rather than silently — `BatchOutcome.escalate` collects ids missing from
the response and ids returned twice with disagreeing verdicts, so a batch the model loses
track of becomes per-item calls, never wrong verdicts — but paying for one extra call
beats leaning on the fallback.

## Cost control

```
monthly_cap        configured, hard — against BILLED spend only
on_cap_reached     degrade to triage-only, log loudly, surface in Sources panel
on_rate_limit      stop the wave, leave items PENDING, retry next sync
per_item_ceiling   skip and park anything whose input exceeds it
```

The cap is enforced in code, not monitored. A pipeline that can silently 10x its spend is
a pipeline you turn off.

It is enforced against money, though, and not against a number that merely looks like it.
A subscription backend bills by the month, and the per-call `total_cost_usd` its CLI
reports is the API-equivalent price of the tokens — an estimate of a charge nobody made,
dominated by the CLI's own session overhead rather than by what Backglass sent. Enforcing
it stopped the ledger for nine runs over nothing.

Removing that brake leaves the subscription's own rolling usage window as the real limit,
which is why `on_rate_limit` exists. It is a different pause and it is told differently: a
limit that parked its items would convert something that clears in hours into permanent
data loss (rule 5, in the direction that matters), and told in the cap's words it would
hand the owner a month-end reset date for a lunchtime wait. `run.degrade_reason`
(migration 0016) is what every surface reads to pick the sentence, and it carries which
stage stopped — `rate_limit:triage` or `rate_limit:extract` — because those leave disjoint
populations behind. A limit during extraction is the cap's shape (triage ran, extraction
did not) and counts kept items with no extraction; a limit during triage is the opposite
(triage stopped, extraction never started) and its items have no verdict at all, so no
count over `triage_verdict = 'keep'` can see them.

The wave stops, but only forward: work already submitted is charged and kept, because
those calls have already spent the window being backed off from. And one unit claiming a
limit is not enough to stop it — a real window refuses every call, so a second claim
always arrives, while a lone claimant is a deterministic failure that read like a limit
and would otherwise stall the queue behind it on every later sync forever.

## Scheduling

| job | cadence |
|---|---|
| ingest | every 30 min |
| extract | queue-driven, as items arrive |
| day plan | 05:45 local, plus a login catch-up if the day has no plan yet |
| brief | 06:00 local |
| evening shutdown prompt | end of working window |
| weekly Monday planning | replaces Monday brief |
| weekly Friday retro | appended to Friday brief |

Local cron or launchd if a machine is always on; GitHub Actions on a schedule otherwise,
which is free and needs no hardware.

Nothing here is real time. An hour of lag is acceptable everywhere.

**One pipeline run at a time.** The ingest job fires every thirty minutes and a person
runs `backglass sync` by hand, so the two overlap — and two runs select the same
`pending_extraction` rows and extract them twice. The ledger cannot catch that: two
extractions of one item are two legitimate-looking commitment inserts, and the only
thing between them and a duplicated ledger is a similarity heuristic. `backglass/
runlock.py` holds an advisory `flock` on `<database>.lock` for the length of a run;
the second run is refused with a sentence and writes nothing. A file lock rather than
a row because the case that matters is the one where nothing gets to clean up — a
sleep, a `kill -9` — and the kernel drops a `flock` however the process dies. A dry
run is exempt: it writes nothing, so it is safe at any time.

## The repair loop

The five stages above put records in. A second pass takes them out again when the record
stops being true, and it is the half that makes the ledger survivable: without it every
obligation ever extracted stays open forever and the board becomes a graveyard nobody
reads.

`backglass/repair.py` owns it as an ordered chain, and the order buys something at each
position:

| | step | what it does |
|---|---|---|
| 1 | `catchup` | fills a hole — the plan or brief a slept-through 05:45 never produced |
| 2 | `replan` | refreshes a plan the day moved under; proposed only, never accepted |
| 3 | `logic` | throws out what the record already contradicts, **before** anything asks |
| 4 | `questions` | asks what is left, having been spared the mooted ones |
| 5 | `notify` | says what the day demands, inside the owner's window |
| 6 | `situation` | renders the state doc, after the repairs have had their say |
| 7 | `vault` | writes the ledger out as markdown — last, because it reports on the rest |

**Two entry points, since 2026-08-24.** The scheduled sync runs it at the end of every
pass; opening the dashboard runs it too, when there is a hole to fill or the loop has not
run in `OVERDUE_MINUTES` (45). The second exists because launchd cannot be trusted to have
fired — on 2026-08-17 the morning jobs had been 12h30 late for weeks, since the agent that
evaluates calendar intervals holds the timezone the machine booted in. It runs on a daemon
thread under the sync lock, so the page never waits for it and two passes can never
overlap.

**A step that fails is reported, not passed over.** Rule 5's unit here is the step: one
repair raising must not cost the other six, but the failure joins the sync's own errors, so
it reaches `run.errors_json` and the Sources panel. This chain previously lived inline in
the sync command with seven bare `except Exception: pass` blocks — a checker that had been
raising for a week was indistinguishable from a ledger with nothing to repair.

No `run` row is written for a repair pass. Four readers take the newest row with no `kind`
filter, so a non-sync row would immediately be reported as the last sync.

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
