# Audit: what the telemetry says, and where the knowledge base actually sits

Written 2026-08-07, one day after Phase 0 landed. Thirty model calls is a small sample
and it is already enough to refute the premise the rest of the plan was resting on.

## What a call costs, measured

| tier | calls | median | p95 | mean chars | imputed per call |
|---|---|---|---|---|---|
| extract | 9 | 39.4s | 53.8s | 8,999 | **17.53c** |
| triage | 20 | 11.6s | 15.8s | 3,168 | 0.83c |
| triage_batch | 1 | 36.6s | — | 4,340 | 2.12c (~12 items) |

Three readings, and the first one changes the plan.

**Cost does not track payload, it tracks output.** A 13,372-char extraction cost 5.7c;
a 7,822-char one cost 22.5c. Duration tracks cost almost exactly (5.2s → 2.5c, 64s →
30c). What the model *writes* is the bill, not what it reads.

**So per-call session overhead does not dominate here.** If it did, cost would be
roughly flat across tiers; instead extract costs 21× triage. `CLI_ISOLATION_FLAGS` is
doing its job and the 2026-07-30 measurement — taken in a different context, on a
different call shape — does not describe this pipeline. **Phase 2.2 (batch extraction)
is withdrawn, not deferred**: batching saves per-call overhead, five items still emit
five items' worth of output, and crowding is how one item's evidence sentence gets
attributed to another. It was a plan built on an unmeasured premise, which is exactly
what Phase 0 existed to test.

**Batching triage is real, and nearly never happens.** 0.18c per item batched against
0.83c per item alone — 4.6× — but one batch call fired across thirteen runs, because
`triage_batch_min` is 4 and a run typically has one to three items pending. Worth
fixing and worth almost nothing: triage is 16.6c of the window's 176.6c.

**Everything is extraction.** A run with no extractions costs about 1c; run 213, with
six, cost 101.6c. At 17.5c a call and 539 historically barren extractions, roughly
**$94 of imputed spend has bought nothing** — and that is the number every remaining
item should be measured against.

## Done in this pass

- [x] Templates learn from the same evidence senders do. `template_verdicts.sql` now
      reports `settled` / `productive` / `unsettled` beside `dropped`, and the tier-0
      rule drops a shape once enough siblings have been ANSWERED — dropped by the model,
      or kept and proved empty by the expensive pass on broadcast-marked mail — with one
      productive or one unanswered sibling disqualifying it. Identical in shape to the
      2026-08-05 sender fix and for the identical reason: "one keep, ever, disqualifies"
      aimed the right instinct at the wrong signal, and marketing mail is kept precisely
      because it is written to look like a deadline.
      **Measured on the live ledger: 18 shapes, zero productive siblings, 98 completed
      extractions that produced nothing — about $17 imputed, already spent, and
      recurring.** Four tests, two mutations proven red.

## Still open, reordered by the measurement

- [ ] 1. **Promote.** `backglass noise suggest` — 142 senders, owner's call.
- [ ] 2. `triage_batch_min` 4 → 2, so batching fires on the runs that have anything to
      batch. Cheap, measured, marginal.
- [ ] 3. Bulk headers at ingest (`List-Unsubscribe` and siblings). The body grep is a
      proxy for a machine-intended marker that the connector currently discards.
- [ ] 4. Phase 3's quality items are unchanged: 32 commitments past due on arrival, six
      duplicate clusters, uncalibrated confidence.

---

# The knowledge base is a notebook beside the pipeline, not inside it

Audited 2026-08-07. `fact` is what CLAUDE.md calls the personal knowledge base.

## What is actually there

Twenty facts across eight subjects — identity, education, premed, housing, preferences,
people, family, orgtruth. All of them true and useful. And:

- **Zero have a `source_item_id`.** Every one was typed by hand. A pipeline that has
  read 8,778 items has contributed nothing to the owner's knowledge base.
- **Zero are superseded.** The supersession machinery has never run.
- **Three readers exist, and none is the pipeline**: `facts.py` (the CLI), the Memory
  page, and the source page's "what came of this item". Neither triage nor extraction
  reads a single fact.

So both directions are disconnected. The model that decides what matters knows nothing
about the person it is deciding for, and the record of that person learns nothing from
the 8,778 documents it has read.

## Why that is the expensive gap, not a cosmetic one

The KB already contains `education.college = ASU Tempe, Barrett Honors, incoming fall
2026`. The ledger contains 95 sender domains and 18 template shapes of *other*
universities' admissions marketing, which cost 436 triage calls and 98 extractions and
produced nothing. A triage pass that knew the college decision was made would drop that
class on sight, as a rule rather than as a per-sender promotion the owner has to
approve one at a time.

That is the argument for integration, and it is also the argument for being careful:
the same fact, wrong or stale, would silently suppress real mail. A KB that steers the
pipeline needs provenance and a review path before it needs volume.

## How it should be implemented and integrated

Read direction first — it is the one with measured value, and it can be built without
touching a prompt.

- [ ] **A. Facts reach the RULE layer before they reach any prompt.** A fact with a
      `rule` shape (a domain, an address, a template class) can feed `rules.classify`
      the way `learned_noise` already does. No prompt version bump, no re-extraction, no
      eval question — and the same suggest/promote gate, because a rule derived from a
      fact is still a rule that can silence a real correspondent.
- [ ] **B. Facts reach the triage prompt as owner context, second.** A short, stable
      block — who the owner is, where they study, what they have already decided. This
      is a prompt change: version bump, whole-ledger re-extraction, and an eval rather
      than a unit test for whether it improves precision. Do it after A has shown the
      facts are trustworthy in production.
- [ ] **C. Extraction writes facts back, last.** `source_item_id` exists on `fact` and
      has never been used. A durable fact learned from mail is a fifth record type
      beside commitment, engagement, checkpoint and evidence, and it needs the same
      treatment: confidence, the review queue below threshold (rule 2), supersession
      rather than edits, and provenance on every row (rule 1). This is the largest of
      the three and the only one that changes the extraction schema.
- [ ] **D. Settle the duplication before any of it.** `identity.emails` and
      `identity.timezones` restate `OWNER_EMAILS` and `DEFAULT_TZ`/`ALT_TZ` from `.env`.
      Two sources for one truth is how they drift; decide which one is canonical and
      have the other read it.

## Deliberately not

A vector store or embeddings over the fact table. CLAUDE.md's one idea forbids it and
it would not help: twenty rows keyed by subject and key are a lookup, not a search
problem, and the queries are known in advance.

---

# Plan: the model backend and the information processing

Written 2026-08-06. Asked to plan how to improve both. Everything below is measured
against a copy of the owner's real ledger (8,778 source items) or read out of the code —
no figure here is an estimate, and where a number is missing that is itself a finding.

## Baseline, as measured

| Fact | Value | How it was measured |
|---|---|---|
| Triage kill rate | 86.3% | `triage_kill_rate.sql` against the live copy; docs/02 expects 90–95% |
| Completed extractions that produced nothing | 539 of 611 | the miner's own query; 417 broadcast-marked |
| Extraction shape | **one CLI subprocess per item**, 6 concurrent | `sync._extract_pass` + `max_concurrency=6` |
| Triage shape | batched, 12 items per call | `triage_batch_size=12` |
| Per-call cost/latency/tokens | **not recorded anywhere** | only `run.spend_cents` exists |
| Imputed spend, last 14 runs | 166c | `SUM(run.spend_cents)`; imputed, never billed |
| Open commitments | 103, mean confidence 0.68 | `commitment` |
| Half-price batch lane | unusable here | needs an API key this machine does not have |
| Template hashes recorded | 4,686 items | `source_item.template_hash` |

Two of those lines carry the whole plan. **The expensive pass produced nothing 88% of the
time it ran**, and **nobody can say what a single call costs or takes**, because the only
telemetry is a per-run total that mixes triage and extraction.

## Phase 0 — Instrument before optimising

Nothing below Phase 1 can be judged without this, and it is the smallest change here.

- [x] 0.1 A `model_call` row per completed call: tier (triage/triage_batch/extract),
      backend, model, item count, prompt chars, wall-clock ms, reported cost, outcome
      (ok / retry / parked / rate-limited). Written on the same seam the run's spend
      already crosses, so no new failure path.
- [x] 0.2 `backglass costs calls` reads it: median and p95 wall-clock per tier, cost
      per call against payload size, and the ratio the 2026-07-30 lesson implies — how
      much of a call is session overhead rather than payload.
- [ ] 0.3 One week of real runs before Phase 2 is designed. Filling from the next sync;
      `backglass costs calls` says "no calls recorded" until then, which is the honest
      answer rather than an empty table pretending to be a measurement. Phase 2's whole premise is
      that per-call overhead dominates; that premise is currently a lesson from a
      different context, not a measurement of this pipeline.

**Proves:** whether batching extraction is worth its risk, and where the wall-clock in a
90-second sync actually goes.

## Phase 1 — Stop paying for mail nobody will ever act on

The precision work. Highest measured value, lowest risk, and half of it already landed.

- [x] 1.1 Barren-keep evidence in `learned_noise` (commit 2af4411). 70 → 140 candidates.
- [x] 1.2 **Ran it** against the real ledger: 142 address candidates and 4 domain
      candidates, and running it is what found the blast-radius defect below. Nothing
      promoted — that decision is the owner's, and the list needs their eye on the .edu
      senders in particular.
- [x] 1.2a **Domain blast radius.** `reply.asu.edu` was offered as a domain candidate
      while Dean of Students, the McKenna programme and the College of Liberal Arts sat
      on it with seven extracted records between them. The per-address disqualifier was
      sound and the promotion it fed was one level wider than the check. A domain is now
      offered only when every address on it qualifies. My own regression, one commit old.
- [ ] 1.2b **Promote.** `backglass noise suggest`, read the evidence, promote. Owner action;
      needs the merge first (migration 18 is not on this branch). Until then all 417
      barren-bulk calls repeat every sync.
- [ ] 1.3 Template-hash drops. 4,686 items already carry a `template_hash` and
      `noise templates` already reports verdict mix per shape. A shape with N sightings,
      zero productive items and zero unresolved keeps is the same promotion argument as
      a sender, one level up — and it catches the sender that rotates its From address,
      which the address rule cannot.
- [ ] 1.4 Capture the bulk headers at ingest. `List-Unsubscribe`, `List-Id`,
      `Precedence`, `Auto-Submitted` are in the `.emlx` and are dropped on the floor: the
      connector stores four keys and none of them is a header. A machine-intended
      broadcast marker beats grepping the body for "unsubscribe", which is what the
      barren rule has to do today. Only helps items ingested after it lands — the ledger
      is immutable — so it is worth doing early or not at all. **Checked before writing
      this:** `content_hash` is computed over author, title, stripped body and
      `occurred_at` only, never `raw_json`, so adding header keys cannot change an
      existing item's hash and cannot trip the 0002 immutability trigger on re-fetch.

**Proves:** kill rate moving toward the 90–95% band, and the barren count falling on the
next sync. Both are already queryable, so the check is a re-run of the same SQL.

## Phase 2 — Amortise what a call costs, once Phase 0 says what that is

Design after 0.3 reports. The candidate shapes, in the order I would try them:

- [ ] 2.1 **Skip extraction the rules can already answer.** Cheapest possible win: an
      item whose template hash has never produced anything does not need the expensive
      pass at all. This is Phase 1's machinery reused at the extraction gate rather than
      the triage gate, and it costs no new model behaviour.
- [ ] 2.2 **Batch extraction the way triage is batched**, small (3–5) and with the same
      escalate-on-doubt contract: anything the batch hedges on is re-read per item at
      full context. The risk is real and specific — extraction is the pass that must
      quote an exact source sentence (rule 1), and crowding items into one call is
      exactly how a model starts attributing one item's sentence to another. So: a
      fixture set that proves per-item provenance survives batching, before any of it
      ships, and an eval rather than a unit test for the quality question.
- [ ] 2.3 Only if 0.2 shows session overhead dominating and 2.1/2.2 are not enough:
      a persistent CLI session. Deliberately last — the 2026-07-30 lesson is that a
      session is exactly what made a 17-token answer cost 29,919 cache-creation tokens,
      and `CLI_ISOLATION_FLAGS` exists to prevent it. Reopening that door needs a
      measurement that justifies it and a test that pins the isolation that remains.

**Proves:** cost per extracted record, before and after, from `model_call`.

## Phase 3 — What the extraction gets wrong

Quality rather than cost. Each of these is a known defect with a live count.

- [ ] 3.1 **32 open commitments were already past due when they arrived.** A 120-day mail
      window reaches back to April, so obligations met months ago land looking open. The
      fix is a rule about the ingest window, not a model change: a deadline that predates
      the item's own ingest by more than the window opens as `archived`, not `open`, and
      says why. Until then the board's Overdue lane is mostly archaeology.
- [ ] 3.2 **Six duplicate clusters, ~13 rows.** The same plan described in mail, in a
      group chat and in a quick-add, worded differently enough that 0.85 fuzzy matching
      misses. The engagement side already learned the answer here — ask the model
      (`replaces_start_at`) rather than tune a threshold. The commitment side has
      `resolves_what` and could carry the same signal for restatements.
- [ ] 3.3 **Confidence is not calibrated.** Mean confidence on open commitments is 0.68
      and the review threshold sits below that; nothing has ever checked whether a 0.68
      is right two thirds of the time. This is an eval, and `evals/` is where it belongs
      — it never gates CI (docs/10).

## Rejected, with reasons

- **A cheaper triage model.** Triage already runs on haiku — and that is the *effective*
  value, not just the code default: the owner's `.env` sets `MODEL_BACKEND` and nothing
  else, so `model_triage=haiku` / `model_extract=sonnet` stand. The waste is not the
  model, it is the 417 calls that should never have been made.
- **Raising the triage keep bar.** triage.md forbids it in terms, and correctly: a false
  negative loses a commitment permanently. Every Phase 1 item improves precision without
  touching the model's instruction to keep when in doubt.
- **The half-price batch API lane.** Needs an API key this machine does not have, and the
  owner's decision on 2026-08-03 was to fix imputed-spend enforcement rather than route
  around it. Nothing here should assume that lane comes back.
- **Reducing `max_concurrency` to save money.** It costs nothing on a subscription; it
  only trades wall-clock. Latency is a Phase 0 question, not a cost one.

## Sequencing

0 → 1 → (measure) → 2 → 3. Phase 1.2 is the only item that produces a saving this week
and it is the owner's to run. Phase 0 is a day's work and everything after it is a guess
without it.

---

# The loop could not learn from its most expensive mistake

Started 2026-08-06. Asked to make the information processing better on the claude_cli
backend. The measurement came first, against a copy of the real ledger.

## What the ledger said

Triage kill rate 86.3% — above the 85% alarm, below docs/02's 90–95% expectation. Counted
through the miner's own query: of 614 kept items, 611 reached a completed extraction and
**539 of those produced no record at all** — no commitment, no engagement, no fact, no
citation, no checkpoint. 417 of the 539 carried an unsubscribe footer. They are college marketing —
scholarship blasts, admissions promos, "Apply in the next 48 hrs" — kept precisely
because they are written to look like deadlines.

Every one of those is a full `claude -p` subprocess. On this backend that is the
dominant cost: the 2026-07-30 lesson measured ~30k cache-creation tokens per call.

`learned_noise` exists for exactly this and held **zero rows**. Its evidence was "the
model dropped it" and its disqualifier was "was it ever kept" — so the senders doing the
most damage were structurally invisible to it, because their mail is *kept*.

## The change

- [x] 1. A second evidence class: a keep the expensive pass **settled to nothing**, on
      mail carrying a broadcast marker. Four conditions, none removable — produced
      nothing, extraction actually ran (`extraction_version`, unset when parked), and
      the marker, which is what keeps this class away from human correspondents. A quiet
      new colleague accumulates barren keeps too; personal mail has no unsubscribe link.
- [x] 2. The disqualifier moves from "one keep, ever" to "anything ever came of it, or
      anything is still open" — commitment, engagement, fact, citation or checkpoint
      disqualifies forever, and an unanswered keep (pending or parked) disqualifies too.
      The bar moves only for keeps the pass has actually answered, so this is not a
      loosening.
- [x] 3. `suggest` prints the two classes apart and a sample subject line. "78 barren"
      and "78 drops" are different observations and the owner is deciding whether to
      stop reading a sender forever.

## Measured against the real ledger, old code vs new

70 address candidates / 1,433 observations → **140 candidates / 3,066**. The newly
visible half is exactly the senders whose mail was being kept and extracted for nothing:
`webmaster@fastweb.com` alone is 62 drops and 78 barren keeps, and was previously
disqualified by those same 78.

## What this is not

Tuning away triage.md's keep-bias. The model's "when in doubt, keep" is untouched; what
changed is that its doubt, once resolved to nothing by the pass that costs real money,
finally counts as the observation it always was. Promotion stays an explicit act, domains
are still never auto-promoted, and `--all` still takes addresses only.

## Found and not fixed

Two display-metadata defects of the same shape, filed together rather than folded in.
`_Tally.see` takes a string min/max over `occurred_at`, which carries mixed offsets —
the shape `tasks/lessons.md` names four times — so the evidence window `suggest` prints
can name the wrong day. And `sample_title` keeps the FIRST barren title it meets while
the query has no ORDER BY, so "arbitrary row order wearing deterministic clothes", the
2026-08-02 lesson exactly. Both misprint a line and change no decision: the promotion
bar reads neither.

---

# What Work & Activities would still be missing

Started 2026-08-06. Third pass on the log zone, and the last one the page needed.

`amcas-export` has always known what each activity is short of — it prints
"Organization: —", "Contact: — (add a supervisor entity)", "no dates logged", and a
zero-character draft. But it only says so when it runs, and there is no reason to run it
until the application is due, by which point the four years it summarizes are over. The
checklist belongs on the page where the record is made.

## Steps

- [x] 1. `activities.export_gaps(row)` — the four fields the export renders a placeholder
      for, named in the order it prints them. `EXPORT_FIELDS` is the one table both
      surfaces read from. `list_with_hours` gains `note_entries` (every checkpoint
      carrying words, not only hour-bearing ones, matching what the export concatenates).
- [x] 2. A Needs column and a "N of M ready" rollup in the registry footer. Muted, no
      chip, nowrap: a gap is a checklist item, not an alarm, and a wrapped two-line cell
      made every incomplete row taller than a complete one, which reads as emphasis.
- [x] 3. `tests/test_amcas_readiness.py` — including the anti-drift test that runs the
      real `amcas-export` through `CliRunner` and asserts every word the page prints is
      a placeholder the export actually emits, and that an activity the page calls ready
      exports with none of them. Three mutations proven red (whitespace org, dates
      requiring a hand-entered start, and a renamed gap word breaking the binding).

## Why a checklist and not validation

AMCAS's own caps are surfaced and never enforced — that is the module's stated rule, and
an activity is perfectly loggable with all four gaps open. The dates rule follows the
export exactly: `started_on` OR a logged span, so an activity with real logged hours
never asks for a start date it does not need.

---

# Hours the bars count and the registry cannot

Started 2026-08-06. Follows the log-zone rework. The table built there put two figures
on one page that disagree: five bars totalling 54 hours, and an activity registry
reading "0 of 15 slots · 0 h". Both are right. The hours are real and they name no
activity, so `amcas-export` cannot see any of them — the page whose whole purpose is
assembling Work & Activities from evidence had none to assemble, and said nothing.

Confirmed on the live ledger before building: 54 logged hours, 54 of them unattributed,
0 activities.

## Why it kept happening

The log form's activity select opens on "no activity" and the field is optional, so the
line a person skips is exactly the one that makes the entry usable later. Nothing
downstream complained, because nothing downstream was looking.

## Steps

- [x] 1. `unattributed(totals)` — summed from the same entry rows the bars and the chart
      sum, not from a new `activity_id IS NULL` aggregate. That keeps it scoped to this
      goal (a second live goal's "Investor conversations" total counts calls, and a
      global sum would fold them into a figure this page calls hours), keeps it clear of
      every timestamp rule, and makes it impossible for the three numbers to disagree.
- [x] 2. `preselect_activity(conn, acts, goal_id)` — `{target_id: activity_id}` for every
      total exactly one activity can feed, so the form already holds the answer where
      there is only one. Two claimants on a total means no default, and the tombstone
      survives a third: ambiguity is not resolved by row order. `goal_id` is passed
      because `total_target_for` without it scans every active goal and picks by id.
- [x] 3. The gap stated where it is discovered — in the registry, muted, only when the
      figure is real, and with no repair button, because the web log form has no date
      field and unlog-and-relog would move July's entries to today. `backglass log --on`
      is the path that keeps the dates, so that is the path named.
- [x] 4. Six tests. Three mutation-proven red: the global aggregate (folds the other
      goal's calls in), the ambiguity tombstone (last activity wins), and both preselect
      cases.

## Verification

1,601 green, ruff and mypy clean. Rendered against the seeded copy in both themes: five
totals each opening on their one activity, "16 unfiled" in the rollup, and the line
under it naming the consequence and the dated repair.

## Deliberately not

Per-entry re-attribution. Fixing the owner's 54 real hours means choosing an activity
per entry while keeping July's dates, which is a new write route with a date field — a
feature, not a UI pass, and the CLI already does it correctly today.

---

# The schedule is the day, not the shift — 12-hour clock and routines

Started 2026-08-06. Two asks, verbatim: times in "am pm system, not army time", and
"the schedule should be longer and more detailed, fitting in time for breakfast lunch
dinner shower, gym among all the other things in life." The second ask settles the
design call the engagement work left open ("what is the schedule page: the working
window, or the day") — it is the day.

## The shape

- **12-hour clock, one formatter.** `panels.clock12/hour12/t12`, a Jinja filter, and
  schedule.py's `_clock` all route through one function — times render "7:30am",
  rulers "8am". Sites: day timeline, week grid, Today panel, gaps, NOW/NEXT strip.
- **Routines are configured fixed events.** `ROUTINES` env
  (`name@HH:MM+MINUTES,...`), defaults covering breakfast, lunch, gym, shower,
  dinner. `capacity.routine_events` renders them as `FixedEvent(kind="routine")`;
  parse and validation share one function, one error type.
- **`capacity.day_events`** = calendar + confirmed engagements + routines,
  deduplicated — the whole day's fixed picture. `compute` clips it to the working
  window for capacity (lunch spends capacity, breakfast does not); the planner
  persists the UNCLIPPED list, so evening plans and morning routines land in the
  plan; the schedule page live-merges the same list for days no planner has visited.
  Routines get no meeting buffer.
- **Ruler already widens** (`_window`) — with a 7:30am breakfast and a 7:15pm dinner
  the day view spans the actual day. `k-routine` renders quiet: dashed rule-colored
  border, paper background — life is ambient, it spends no reserved ink.

## Steps

- [x] 1. `config.routines` + `config.parse_routines` (one parser, one error type;
      lives in config so the field validator avoids a circular import).
- [x] 2. `capacity`: `FixedEvent.kind`, `routine_events`, `day_events`, `compute`
      over it, zero buffer for routines.
- [x] 3. `planner.propose` persists the unclipped day picture with each event's kind.
- [x] 4. Schedule route: `day_view` reads `day_events` (evening engagements appear —
      closes the documented gap); 12-hour labels; `unplanned` note so the planner
      hint survives a day routines keep non-empty; week grid needs a non-routine
      entry to earn its ink. The 12-hour clock lives in `plan/timezones` so the CLI
      plan printout and the brief's plan lines use the same one.
- [x] 5. Templates + CSS: rulers, Today panel filter, `k-routine` styles, legend.
- [x] 6. Tests: `tests/test_routines.py` (parser, capacity arithmetic, 12am/12pm
      boundaries, page + week + evening engagement); 24h assertions updated in
      test_web_pages/test_schedule/test_planner to the new expected shapes.
- [x] 7. Full suite (1483 passed), ruff, mypy — green minus the three pre-existing
      defects at bb32d2d; fresh verifier pass CONFIRMED.

## Verification outcome

Fresh verifier confirmed on first pass: clock12 swept all 1440 minutes against
strftime with zero mismatches; capacity arithmetic, confidence gating, dedup after
persist, planner-rerun supersession, DST spring/fall days, alt-tz stays and a
midnight-crossing routine all reproduced. Its flags, acted on: the "05:45" prose
this work itself wrote is now "5:45am" on every surface (panels, brief alert, three
templates, CLI help). Noted, not changed: the brief's Today section now carries the
five routine lines daily (consequence of "the plan is the day" — a product call the
owner can reverse by blanking ROUTINES); overlapping routines double-count
fixed_minutes (pre-existing compute behavior for any overlapping fixed events, errs
conservative); editing ROUTINES after a day is planned shows both copies until the
next plan supersedes — visible, not silent.

## Deliberately not

Per-weekday routine schedules and a routines UI — the env default is a template the
owner edits once; a table and page would be a settings shrine for five rows. No
capacity charge for routines outside the working window: the window is still what
bounds work, routines outside it are life, not spend.

---

# Major decisions — the choices the owner has settled

Started 2026-08-06. The ask, verbatim: "there needs to be a major decisions list as
well — for example Sallie Mae application is something I don't want to do." A decision
like that is not an open commitment (nothing left to do), not a done one (nothing was
done), and not quite a fact (it has a lifecycle against the ledger: the Sallie Mae
commitment should close when the decision lands). It is the fact pattern — supersession,
retraction, provenance-in-words — plus one link into the commitment ledger.

## The shape

- **`decision` table** (migration 0017): title ("Sallie Mae application"), choice ("not
  doing it"), reasoning, optional `commitment_id`, active|superseded|retracted with
  `superseded_by`, decided_at. Same lifecycle rules as `fact`: never UPDATE a claim,
  changing your mind is recording the replacement; identity for supersession is the
  normalized title, compared in Python only (one `_norm`, per the Café Latino lesson).
- **Recording a decision can close the commitment it settles.** Optional — a decision
  needs no commitment — but when linked and the commitment is open, it is dropped with
  `resolution_note = "decision:<id> — <choice>"` in the same transaction. Revisiting
  (retracting) a decision does NOT reopen the commitment: resurrect-by-side-effect is
  the resurrection bug class, and drop already has its own recovery story.
- **`/decisions` page** mirroring Memory: list newest-first, add form with an optional
  "also closes" select over open commitments, Revisit button. Nav entry + key '9'
  appended after Brief so existing keys 1–8 keep their meaning.
- **CLI** `backglass decisions` (list) / `decisions record TITLE CHOICE [--why] [--closes N]`
  / `decisions revisit ID`, same shape as `backglass memory`.

## Steps

- [x] 1. Migration `0017_decisions.sql`, FROZEN_CHECKSUMS entry, regenerate
      `specs/schema.sql`.
- [x] 2. `backglass/decisions.py` — record (with supersession + optional commitment
      close), active, revisit.
- [x] 3. `backglass/web/routes/decisions.py` + `decisions.html`, wired in `app.py`,
      nav in `base.html`.
- [x] 4. CLI sub-app in `__main__.py`.
- [x] 5. `tests/test_decisions.py` — engine + page, including: case-insensitive
      supersession, linked-commitment close, closed-commitment left untouched,
      revisit does not reopen, blank 422, unknown ids 404.
- [x] 6. Full suite + ruff + mypy in the worktree (green minus three defects
      pre-existing at bb32d2d: the tracking-pixel test, 9 ruff errors, one
      apple_mail mypy error — all reproduced at clean HEAD); fresh-context
      verifier pass.

## Verification outcome

One verifier pass, one refutation, narrow and real: `record()` claimed "same
transaction" over an autocommit connection with no BEGIN — an interrupt between
the INSERT and the commitment drop left a standing decision over a still-open
commitment, or two actives sharing a title. Fixed with BEGIN IMMEDIATE /
COMMIT / ROLLBACK (the people/merge.py shape), proven by a temp trigger
aborting the final write and a mutation run going red without the wrapper.
Secondary: linking an already-closed commitment rendered "closed:" — an act
that never happened — now "re:"; and the CLI door had zero tests (a guard on
one door is not a guard — the rule applies to tests too). 1459 passed after
the repairs. Everything else the verifier probed independently — checksum
seal, schema parity, 16→17 upgrade idempotency, both doors' refusals,
multi-active supersession — confirmed.

Built in worktree `decisions-ledger` (branched from bb32d2d) because two other live
sessions share the main checkout and its tree carries a 700-line uncommitted diff.

## Deliberately not

Auto-suppressing future extractions that mention a decided topic — a re-extracted
"apply to Sallie Mae" commitment stays visible and droppable, because a visible
duplicate is dismissible while an auto-suppressed real obligation is silent data loss
(the same consequence call the engagement dedup settled on). No `source_item_id` on
the table: decisions are owner-typed like quick-adds; evidence lives in words and in
the linked commitment's own citation chain.

---

# Polish pass — the states nobody drives, and the values nobody types

Started 2026-08-05. The ask: audit the whole app, check every state, check the edge
cases. Method: two adversarial sweeps rather than a reading of the code — one over every
route against an empty ledger with hostile path/query params, one over every write
against a ledger seeded with one of everything. 67 GETs + 205 POSTs, then 60 more writes
with out-of-range values. The suite was green (1425) before and says nothing about any
of this, because every test drives a value someone chose to write down.

## What the sweeps found

Six defects, in severity order. Two of them lose data.

1. **A malformed date is a traceback, in six places.** `date.fromisoformat` is called on
   user input at `web/routes/schedule.py:419` (`?date=`), `:444` (`?start=`) and at four
   CLI `--date` options — and exactly one caller in the tree (`log --on`) explains
   itself. `/schedule?date=2026-02-30` is a 500. So is a stale bookmark, a hand-edited
   URL, or a typo in `backglass plan --date`. `/brief/{on_date}` gets this right by
   accident of typing its parameter `date`, which is the fix the others want.
2. **Snooze erases the deadline.** `date(base, '+N days')` returns NULL when SQLite's
   date arithmetic overflows, and `snooze()` bounds `days` below (`>= 1`) and not above.
   `POST /commitments/1/snooze/1000000000000000` sets `due_at = NULL` and reports
   "snoozed". The commitment stays open with no date, on a board that sorts by date.
3. **Quick-add writes an unvalidated due date.** The form field goes to the ledger raw:
   `tomorrow`, `2026-02-30`, `9999-99-99` and 300 characters of `x` all land in
   `commitment.due_at`, a column every board query orders and compares by. The
   extraction door already resolves through `dates.resolve_due`; the owner's own door
   does not.
4. **Twenty routes 500 on a large id.** FastAPI's `int` is unbounded, SQLite's is 64-bit,
   so `/people/999999999999999999999999999999` is an OverflowError rather than a 404.
5. **Ticking a checklist item that is gone is a 500**, not the 422 every other stale-row
   write returns — `tick`/`untick` are the only two actions that write without checking
   the row exists, so the FK failure escapes as an IntegrityError.
6. **No upper bound on an estimate, a weekly count, or quick-add's text.** A
   4.6-quintillion-minute estimate is stored and then fed to the planner's capacity
   arithmetic; 20,000 characters render as a board row.

## Steps

- [x] 1. One way to read a day from user input. `parse_day()` in `backglass/dates_cli.py`
      (or the nearest existing home) raising one error type with the message `log --on`
      already gives; every CLI `--date` uses it. Web routes annotate the parameter `date`
      so FastAPI answers 422, and the prev/next arithmetic clamps at `date.min`/`date.max`
      so `9999-12-31` is a page and not an OverflowError.
- [x] 2. `snooze` bounds `days` above as well as below, and the bound is a real one
      (a snooze is a working-life gesture, not a century).
- [x] 3. Quick-add routes `due_at` through `dates.resolve_due` against the owner's local
      now — so "friday" works, and unresolvable text is a 422 rather than a silent
      corruption. `Ledger.insert_commitment` refuses a non-ISO `due_at` regardless of
      door, because that is where the data is written.
- [x] 4. One bounded id type for every integer path parameter.
- [x] 5. `tick`/`untick` check the item the way every other action checks its row.
- [x] 6. Upper bounds on estimate minutes, weekly count, and quick-add text length.
- [x] 7. A test per defect that has been watched to fail against the current code, and
      the sweeps kept as `tests/test_edges.py` so the next value nobody types is caught
      by CI rather than by a sweep.

## Outcome — part two, the interface

Six more commits, same branch, same method: measure first. A headless browser loads
every route at five widths and in both themes; axe-core audits each at WCAG 2.1/2.2
AA. Suite 1,498 → 1,510. Ten more mutations, ten red.

The four that mattered, all of them functionality rather than taste:

- **On a phone the board was read-only.** Resolve, Snooze and Drop are revealed on
  hover, and a touch device has no hover — so the one surface whose whole purpose is
  acting on a row could only be read. `@media(hover:none)` makes them present, the way
  the Today rows' controls always are.
- **The dashboard rendered 642px wide inside a 390px window.** One mechanism in four
  places: a hard pixel floor inside a grid or flex track, and a grid item's automatic
  minimum is its min-content width. The right third of every panel was unreachable.
- **The sidebar hid every alert below 900px.** Goals and Roadmaps yielding there is
  right — both summarize a page one tap away — but alerts have no page of their own,
  so a failing source was invisible on a phone.
- **The second `x` resolved a row nobody had selected.** A write swaps the panel and
  discards the DOM the selection lived on, but `index` is module state and survived,
  pointing into a list that had just shifted up.

And three of craft: no page had a `<main>`; muted text failed AA on every fill in the
interface (the ramp's ratios are quoted against paper, and every fill is darker than
paper); a 16px day box became a 26px target without the mark changing size.

## The backend, exercised rather than assumed

Asked to confirm the backend works, not only the interface. Nothing needed fixing —
recorded so the next session does not repeat it.

- **Every CLI command, against a fresh database.** Twenty-seven of them: no traceback
  anywhere, and every non-zero exit carries a sentence naming what to do
  (`IMESSAGE_DB_PATH is not set — run backglass setup`, `no activity matching
  'shadowing' — add it with …`). The four that looked like failures were an honest
  "not configured yet" or my own shell quoting.
- **The run lock under a genuine two-process race**, not a same-process descriptor:
  two real `sync()` runs on one database, one ran and one was refused by pid and start
  time, and the refused one wrote nothing.
- **Concurrent writers.** The launchd sync writes every thirty minutes and the owner
  clicks while it runs, so the question is real. Python's sqlite3 already carries a 5s
  busy timeout and `connect()` opens in autocommit, so no write transaction is held
  long enough to collide: three processes, nine thousand writes, zero `database is
  locked`. The one failure I could produce needed a `BEGIN IMMEDIATE` held open past
  the timeout on purpose, which nothing in the pipeline does.
- **A brief against a populated ledger** — the degraded-source warning at the top
  (rule 5), a provenance link on every line (rule 1), capacity, and awaiting-others.

## The three open calls, decided

Left open for the owner in the first pass; taken here on 2026-08-06 when asked.

**The duplicate run lock — theirs stays.** Both sessions built one, independently, to
nearly the same design. Theirs reached main first and covers the same three doors, so
`backglass/runlock.py` and `tests/test_runlock.py` are deleted rather than a committed,
integrated, passing feature being replaced by its twin. Two things came across, because
they are where the implementations actually differed: a real second process in the
tests (their four stand-ins all pass against `LOCK_EX` weakened to `LOCK_SH`, which is
the entire defect), and a reentrancy depth keyed by database rather than counted once
for the process. See the merge commit.

**Black on vermilion at 4.40 — the ink stays, the claim changes.** Reaching 4.5 means
darkening the alarm ink, and a desaturated overdue mark is a quieter alarm: a worse
outcome than a 2% shortfall on text that already carries a keyline, a glyph and a word.
The all-pairs CVD separation was validated at these exact values, so moving a series
slot would have to be re-argued against deuteranopia and not just against WCAG. What
was actually wrong was §3's sentence claiming gold was the only exception;
`design/design-system.md` now documents both, and says precisely where the second one
bites — normal text on full-strength vermilion, dark mode only.

**The week grid's 22px links — exempt, and written down as exempt.** Height is duration
there; padding the link would make the timeline lie. §7 now carries the rule the 26px
day box already follows (targets grow *under* the mark, never around it) together with
the two exemptions that apply here, so the next reader does not re-open it. If the
touching neighbours ever want fixing, the fix is a gap in the grid.

## Outcome — part one, the ledger

Four sweeps, three commits, on branch `polish/edge-states` in a worktree — another
session was editing the shared checkout mid-audit, which is written up in
`tasks/lessons.md`. Suite 1,441 → 1,498. Twenty-seven mutations, twenty-seven red, in
three passes: the first pass of each left survivors, and one of those survivors was a
defect rather than a missing test.

1. **Six defects in the values a URL and a form can carry** (commit 1). Two lost data:
   a snooze large enough to overflow SQLite's date arithmetic erased an open
   commitment's deadline and reported success, and quick-add wrote its due field into
   the ledger as typed, so `tomorrow` and `2026-02-30` became due dates in the column
   the board sorts by. The rest: a malformed date was a traceback in six places, twenty
   routes 500'd on a large id, ticking a vanished checklist item was a 500, and nothing
   bounded an estimate, a weekly count, or a commitment's length.
2. **Two syncs can no longer run at once** (commit 2) — the defect this file left open,
   closed with an advisory `flock` rather than a row, because the case that matters is
   the one where nothing gets to clear the row.
3. **A confirmed dinner is on the day, and one unreadable row is not a blank page**
   (commit 3). The Schedule page never read engagements at all; and a `plan_block`
   timestamp that is not full ISO took down the day view and the week grid's other six
   days with it.

## Swept and found sound

Recorded because a negative result is a result, and re-sweeping these is wasted effort:

- **Every temporal edge on the schedule surface** — an event that ends before it starts,
  a zero-length one, one spanning midnight, a 25-hour one, one written in the other
  timezone, one with no offset at all, a date with no time, an empty title, 5,000
  characters of title, markup in a title (escaped correctly), two events at the same
  minute. All render.
- **Goals and roadmaps against the shapes progress arithmetic divides by** — a goal with
  no targets, a NULL weekly count, a weekly count of zero, NULL minutes-each, a
  milestone with a total of zero, a total already exceeded, checkpoints at ±the bound, a
  roadmap with no steps, one with every step done, one whose goal is dropped, two
  targets differing only by case, a step with no planned date. No 500s, no unescaped
  markup, no division by zero.
- **The first run a stranger gets.** `init` then `doctor` on an empty database: five
  failing checks, every one naming the environment variable or command that fixes it.
  `plan`, `brief`, `status`, `costs`, `people`, `memory export` all render an empty
  ledger without complaint.

## Left alone, deliberately

- **`engagement.done` is still unreachable** — migration 0014 advertises the state,
  nothing can reach it. It needs a write surface (a "went" button and its route), which
  is a feature and a product decision about where that button lives, not a polish fix.
  Still worth doing.
- **The brief, the connectors and the security middleware.** A second session was
  auditing exactly those files in the shared checkout while this ran; touching them
  would have raced it.
- **`tests/test_dashboard.py::test_the_tracking_pixel_records_the_first_open_only` is
  red at HEAD** — `mark_brief_opened` requires `sent_at IS NOT NULL` and the committed
  test never sets it. Not fixed here because the other session's uncommitted tree
  already fixes it, and two fixes would conflict.

---

# The hour log reads like a spreadsheet, not a record

Started 2026-08-06. The owner asked for the roadmap page to take after two things: a
Notion database view, and the hour-logging charts a pre-med keeps for AMCAS.

## What the page is missing

The log zone has five progress bars and a list of activities as stacked prose blocks.
Both say *how far*; neither says **whether logging is still happening**. A research
total sitting at 60/200 renders identically whether the last entry was yesterday or
last spring — and on a four-year accumulator that is the question the page exists to
answer. There is no time dimension anywhere on the page.

The activity registry has the same problem in the other direction: it holds exactly the
columns a database view is made of (category, org, role, hours, entry count, date span,
most-meaningful flag) and renders them as sentences, so nothing is comparable down a
column and there is no rollup.

## Steps

- [x] 1. `backglass/goals/hours.py` — bucket every total's checkpoints by the month its
      own timestamp names, in the offset that timestamp carries. Never converted, never
      compared: each stamp is parsed once and reduced to (year, month), which keeps the
      module clear of the mixed-offset trap four lessons already describe.
- [x] 2. Logged-by-month chart in the log zone (`_hours_chart.html`). One series, black
      columns (§5: a single-series chart uses black, not cobalt). The hours-a-month
      needed to reach the goal's target date is a dashed hairline reference — structure,
      not a second series, so the three-series rule and the status/series split both hold.
- [x] 3. Activity registry → a table with column headers and a rollup footer, using the
      existing `.wkg` table idiom rather than a new one. The fold's summary carries the
      rollup so the counts read without opening it.
- [x] 4. Context through `progress_context` only — `#roadmap-totals` is swapped whole by
      six POST handlers, and a chart that renders on GET and vanishes on the first logged
      hour is the obvious way to ship this broken.
- [x] 5. Tests: bucketing across the owner's two zones, the empty case, the pace line's
      arithmetic, and the chart surviving a log POST's fragment render. 20 new, and the
      two that carry the argument were mutation-proven red (convert-to-UTC before
      bucketing; drop the minimum-bar floor).

## Outcome

1,483 green, ruff and mypy clean on the changed files. Verified in Safari against a
seeded copy of the demo db — never `data/backglass.db` — chart and table each rendered
in both themes: 220 hours over ten months, a 63-hour July peak, and the 8/mo pace line
reading across the plot. The axis-lift case was rendered too, not just asserted: with
the goal's target pulled to October the pace becomes 170/mo, the axis lifts to it, and
the line sits at the top of the plot with every column correctly dwarfed beneath it.

Three things the render caught that the markup did not:

- The current month drawn as a mild ground behind its column read as a second, lighter
  bar at full height — the one thing a column chart must never draw. It moved to the
  axis, where "this one is now" is a rule under the label.
- The pace line was occluded by every bar it is a reference for, so it appeared only in
  the two months that happened to be empty. It now sits above the columns.
- `.wkg` zeroes `padding-left` on every `.nm` cell, which is right for a table whose
  name column is first and wrong for one with four columns reading left: ENTRIES ran
  into SPAN with no gutter at all.

## Follow-up, found and not fixed

`activities.list_with_hours` takes `MIN(occurred_at)`/`MAX(occurred_at)` over the
mixed-offset column to compute each activity's span — the aggregate-is-a-comparison
shape `tasks/lessons.md` names on 2026-08-02. The consequence is bounded (a span
endpoint can name the adjacent day when two entries straddle midnight across the
owner's two zones) and the fix means fetching entries rather than aggregating in SQL,
so it is written down rather than folded into a UI change.

## Deliberately not

A second chart. The per-total bars already answer "by category, versus target", which is
the other chart a pre-med keeps — restating it in a second shape would be more ink for
the same fact, against a week of owner rulings that all moved toward less.

---

# Populate the ledger — mail, messages, and the cap that was never real

Started 2026-08-03. The ask: put the owner's actual information into the system, from
every source this machine can reach. Four things stood between the ledger and that.

## What the analysis found

- **Mail was missing entirely** — the largest source of a person's commitments, absent
  since the Google OAuth path was never usable here. But `~/Library/Mail/V10` holds
  35,376 indexed messages and 35,441 `.emlx` bodies across three live accounts
  (personal Gmail, ASU Gmail, iCloud), synced minutes ago. The same shape as the
  Calendar.app discovery: the API for a service and the data from it are different
  questions. The ASU account carries Canvas notifications, so mail also covers Canvas
  without a Canvas token.
- **iMessage read nothing.** `monitored_chat` was empty and `IMESSAGE_CHATS` unset, so
  the allowlist was empty, so `health()` failed, so `sync` skipped the connector — and
  the skipped connector is the only thing that records sightings. The page that exists
  to fill the allowlist could never fill, because filling it required the connector the
  empty allowlist had already disabled.
- **The spend cap was phantom.** `MODEL_BACKEND=claude_cli` is subscription auth; the
  CLI's `total_cost_usd` is an API-equivalent imputed price, not a charge. `SpendCap`
  enforced it as one and degraded nine consecutive syncs to triage-only over money
  nobody was billed.
- **The data boundary was undecided** — `BOUNDARY_MODE=exclude` with an empty denylist,
  which excludes nothing. docs/08 requires this settled before mail ingestion.

## Decisions the owner made

Mail: build it, all three accounts. Boundary: no client correspondence in these
accounts (they are the student's own; the inbox docs/08 was written about is not
connected). Chats: the three busy groups plus Family. Spend: raise to $50 — and since
the batch lane needs an API key this machine does not have, fix the imputed-spend
enforcement rather than route around it.

## Steps

- [x] 1. Spend truthfulness. `ModelClient.spend_is_imputed` — every backend answers, and
      `SpendCap` stops work only on billed spend while still recording either. Dropped
      `--max-budget-usd` on the subscription path; `costs` and `doctor` now say which
      kind of number they are printing. Cap raised to 5000c.
- [x] 2. iMessage discovery. `discover()` scans the whole window instead of the fetch
      loop's cursor-bounded rows, which is what broke the deadlock; `record(cumulative=
      False)` because a window total is not an increment; `decide(MONITOR)` rewinds the
      source so saying yes reaches backwards. 47 conversations now on the page.
- [x] 3. The four chosen conversations set to `monitor` — Pih ball, SLT, plague
      spreaders, Family. The other 43 remain undecided and unread.
- [x] 4. `apple-mail` connector, the full checklist: connector, config gate,
      `.env.example`, registry, detection, docs/07 section, boundary, 23 tests. Live
      against the real store: 226 messages in seven days, offsets intact.
- [x] 5. Boundary decision recorded in docs/08 §The decision as made, with
      `BOUNDARY_OUT_OF_SCOPE_ACCOUNTS` enforcing it in the connector before persistence
      and `doctor` printing every mailbox the ledger has read.
- [x] 6. Run it. Two syncs: the first ingested 4,417 mail items and triaged all of them
      before dying on a `TypeError` in `_minutes_apart` — offset-aware minus
      offset-naive, inside a `try/except ValueError` that could never catch it. Fixed
      that, and fixed the reason one item could end a run at all (`apply()` was the only
      path in the pipeline that re-raised). Second sync: **844 extracted, 0 parked**.
- [x] 7. **Reloaded `com.backglass.sync`.** Unloaded for the backfill, because nothing in
      the codebase stops two syncs running at once and the launchd job fires every 30
      minutes: two concurrent extraction passes over the same pending items produce
      duplicate commitments, which only the 0.85 dedup would catch and only sometimes.
      That missing lock is a real defect and is written up below rather than fixed here.

## Outcome

Source items 4,067 → **8,485**. Open commitments 43 → **94**, engagements 110 → **200**,
entities 57 → **88**. Every commitment carries evidence — zero rows without provenance,
which is rule 1 holding under a tenfold ingest rather than in a fixture. All seven
connectors green; the last run degraded nothing.

What the backfill also produced, said plainly rather than left for the owner to find:

- **32 open commitments are already past due.** A 120-day mail window reaches back to
  April, so obligations that were met months ago arrive looking open. They are real
  extractions of real messages; they are just answered already.
- **Six duplicate clusters, ~13 rows.** The same plan described in mail, in a group chat
  and in a quick-add, phrased differently enough that the 0.85 fuzzy dedup did not join
  them. This is the known limit of similarity matching, not a new defect.
- **57 sit below the confidence threshold** and are in the review queue rather than in
  the brief, which is rule 2 working.

## Found and not fixed

**Two syncs can run at once — FIXED 2026-08-05.** `sync.run_lock` holds an advisory
flock on `<db>.sync-lock` for the length of a run; `sync()`, `batch.submit()` and
`batch.collect()` all take it (submit nests around the `sync(extract=False)` it calls).
A refused run raises `SyncLocked`; the CLI prints who holds the lock and exits 0 — a
launchd fire mid-backfill is a clean skip, not a failure. flock releases on process
death, so a crashed run cannot strand it. `tests/test_sync_lock.py` covers both
branches plus reentrancy, mutation-proven red; refusal verified live against a running
sync (pid printed, no run row written).

**Fixed 2026-08-05** — `backglass/runlock.py`, the lock file rather than the row: the
case that matters is a sleep or a `kill -9`, where nothing gets to clear a row and a
stale one converts an occasional duplicate into a permanent outage. Taken inside
`sync()` and `batch.collect()` rather than at the CLI, because there are three doors
into the pipeline and a guard on one of them is not a guard; reentrant, because
`batch submit` calls `sync`. A dry run is exempt. `tests/test_runlock.py`, ten
mutations, ten red — including one that only a real second process could catch:
weakening `LOCK_EX` to `LOCK_SH` is the entire defect and passed every same-process
test in the file.

## Deliberately not

A Canvas connector — mail already carries the notifications, and a token the owner has
to fetch is a worse trade than a store already on disk. Instagram — the export is not on
this machine. Reading the parent's inbox, ever.

---

# Monitored conversations — the owner decides what is watched

Started 2026-08-03. `IMESSAGE_CHATS` and `INSTAGRAM_CHATS` are comma-separated env vars,
so choosing what the ledger reads means hand-editing `.env` with names guessed from
memory. Worse, a conversation the allowlist does not name is dropped in silence: a new
group chat where plans are actually being made never surfaces, and the owner has no way
to know they are missing it.

## The shape

The same shape the review queue already has, and for the same reason: the system finds
something, the owner decides once, the decision is remembered. A chat is not a setting to
be typed, it is a decision to be made.

- **`monitored_chat` table.** (source, key, display_name, kind, decision, counts, dates).
  `decision` is `monitor` | `ignore` | NULL, and NULL is the whole feature — it means
  *seen but not yet decided*, which is what raises the prompt.
- **Connectors report sightings.** They already count `excluded_by_rule["allowlist"]`;
  they will also record *which* conversations they saw, on the same
  connector-reports/sync-writes seam that `excluded` uses. A connector still never writes.
- **The allowlist comes from the table**, falling back to the env var so nothing breaks
  the moment this lands. Env entries are seeded into the table as `monitor` on first run,
  which is also the migration path.
- **Undecided chats raise a prompt** — a dashboard panel, exactly like Needs review, with
  Monitor / Ignore as equal-weight buttons. Ignoring is a real decision and is remembered,
  so the same chat is never asked about twice.

## Steps

- [x] 1. Migration `0015_monitored_chats.sql` + regenerate `specs/schema.sql`, add to
      `FROZEN_CHECKSUMS`.
- [x] 2. `backglass/chats.py` — read the allowlist from the table, record sightings,
      apply decisions. One module both connectors and the web layer use.
- [x] 3. iMessage + Instagram: record every conversation seen, allowed or not.
- [x] 4. `sync.py` writes the sightings; new ones land undecided.
- [x] 5. Dashboard panel + `/chats` page with Monitor/Ignore, and routes for the decision.
- [x] 6. Seed from `IMESSAGE_CHATS`/`INSTAGRAM_CHATS` so existing config keeps working.
- [x] 7. Tests at every step; docs/07 gains the flow.

## Outcome

Run against the owner's real store: **47 conversations discovered**, every one awaiting a
decision — three busy group chats, the rest one-to-one. None is read until it is chosen.

Instagram joined 2026-08-05: both lanes report sightings under the shared source
`instagram` (export lane discovers the whole export cursor-free; the live lane sights
every thread it lists without fetching unallowed ones — the ban-risk budget pays for
nothing nobody said yes to). The allowlist now comes from the table with
`INSTAGRAM_CHATS` seeded on first run, an empty allowlist no longer disables the
connectors, and Monitor rewinds both lanes' cursors so a decision reaches backwards on
whichever lane carries the conversation.

## Deliberately not

Auto-monitoring anything. A new chat is a question, never a default — the whole point is
that the owner has not consented to it yet. And no per-message opt-in: the unit of consent
is the conversation, because that is the unit a person thinks in.

---

# Engagements — the plans people make with each other

Started 2026-08-02. The owner asked for four things: connect every social account, build
a profile for each contact, identify plans with friends and professional events, and plan
the day around them. A survey of the tree found that two of the four already exist, one is
an activation problem rather than a build, and exactly one is genuinely missing.

## What the survey found

| Ask | State | Evidence |
|---|---|---|
| Plan the day for you | **Built and running daily** | `backglass/plan/planner.py:223`, day_plan rows for 2026-08-03, launchd `com.backglass.plan` at 05:45 |
| Profile per contact | **Half built** | `backglass/people/` has search, timeline, touch/staleness, merge, a `/people/{id}` page and a brief section. But nothing derives a profile — 0 of 26 entities have `profile_json`, and every `role`/`org` present was typed by hand |
| Connect social media | **Built, not activated** | iMessage, Instagram (export + live) and Slack connectors are complete and registered (`backglass/__main__.py:583-628`). None is running |
| Identify plans / events | **Missing entirely** | `extract/schemas.py` defines five models, all commitment- or triage-shaped. `extract/rules.py:120` *drops calendar invites at tier 0*. There is no record type for a plan with a person |

Two findings reframe the request and belong at the top:

1. **Gmail has never been connected.** `.env` has no `GOOGLE_CLIENT_ID`; the credential
   table holds no google row; `source_item` holds zero gmail items. The 200 `calendar:asu`
   rows were hand-imported. CLAUDE.md's build order calls Gmail extraction Phase 1, and it
   is dark. Most invitations — a friend proposing dinner, a recruiter proposing a call,
   Meetup/Eventbrite/LinkedIn notifications — arrive by mail. No amount of extraction work
   pays off until this source is live.
2. **`detect.py` reports iMessage as `configured` when it is not readable.** The file
   exists (101 MB, modified today) so the path check passes, but the process has no Full
   Disk Access and the real fetch dies with `OperationalError: unable to open database
   file` — which is what the credential row records. A check that passes on a source that
   cannot be read is the exact shape `tasks/lessons.md` warns about: a check nobody has
   seen fail.

## What "all social media" can honestly mean

Not every platform is reachable, and pretending otherwise would build scrapers that break
or violate terms. The defensible split:

- **Reachable, already built, needs activation:** iMessage (local SQLite, needs Full Disk
  Access), Instagram (Meta data export, or the experimental live lane), Slack (user token).
- **Reachable, not built:** Telegram has an official user-level API. Discord and WhatsApp
  both offer a personal data export that is a file-parsing job of the same shape as the
  Instagram export lane.
- **Not reachable on defensible terms:** LinkedIn and X forbid scraping / paywall the API;
  WhatsApp and Signal live traffic is end-to-end encrypted with no supported local read.
  **These reach us as email.** LinkedIn invitations, Meetup RSVPs, Eventbrite tickets and
  Facebook events all arrive in the inbox, which is another reason finding #1 dominates.

So the plan does not add six connectors. It makes the sources that exist produce the
record the owner actually asked for, from whatever channel carries it.

## The one idea

An **engagement** is a plan involving other people at a time: dinner with a friend, a
conference, an interview, office hours. It is a first-class typed record beside the
commitment — same ledger, same provenance rule, same review queue. This is not a new
subsystem; it is a second noun in the one the project already has.

Critically it is extracted by the **existing** tier-2 pass, not a new one. Adding a second
model call per item would double extraction cost and violate the two-tier design in
`docs/02`. `extract-commitments.md` gains an engagements block and a version bump; the
bump re-extracts the ledger once, which is free under `MODEL_BACKEND=claude_cli`.

## Steps

- [x] 1. **Make the activation state honest.** `detect.py` must *open* the iMessage db, not
      stat it — report `needs_setup` with the Full Disk Access instruction when the read
      fails. Both branches get a test (lessons.md: a check earns a test for its pass *and*
      its fail branch). Same treatment for any other detector that only checks a path.
- [x] 2. **Migration `0014_engagements.sql`** — `engagement` (user_id, kind
      social|professional, what, starts_at, ends_at, when_is_explicit, location, status
      proposed|confirmed|declined|done, confidence, source_item_id, created_at) and
      `engagement_person` (engagement_id, entity_id, role) so one plan can involve several
      people. Add to `FROZEN_CHECKSUMS`, regenerate `specs/schema.sql`.
- [x] 3. **Schema + prompt.** `ExtractedEngagement` in `extract/schemas.py`;
      `CommitmentExtraction` gains `engagements: list[...]`; the `## Output schema` block in
      `extract-commitments.md` gains the matching block and the version bumps. The two must
      change in the same commit — that file's header says so.
- [x] 4. **`extract/engagements.py::apply()`** mirroring the commitment post-processing:
      resolve each participant to an entity, resolve the time against the *source item's*
      timestamp (rule 4), dedup against open engagements, route low confidence to the review
      queue (rule 2), record the evidence sentence (rule 1). Idempotent — a second run over
      the same item writes zero rows (rule 3).
- [x] 5. **Planner integration.** A confirmed engagement with a time is a `fixed` block in
      the day plan, exactly as a calendar event is; `plan/capacity.py::fixed_events()` is the
      single place that has to learn about it. A proposed one that has gone unanswered
      surfaces in the brief as something owed a reply.
- [x] 6. **Derived contact profiles.** Query-time, no new table — consistent with how
      `people/touch.py` and `people/timeline` already work. Per person: channels seen on,
      first and last contact, interaction count, social-vs-professional lean derived from
      engagement kinds and org presence, upcoming engagements. Rendered on `/people/{id}`.
- [x] 7. **Docs and wiring contract.** `docs/03-data-model.md` gains the two tables,
      `docs/07-connectors.md` gains the activation section for the three dark social
      sources, `docs/04` gains the engagement input to the planner. `tests/test_connectors.py`
      already asserts the registry and docs ends mechanically — keep it green.

## Verification

`uv run pytest` green, ruff and mypy clean, and — the one that matters — a real engagement
extracted from a real message appears as a block on the day plan with a working provenance
link back to the sentence it came from. Idempotency asserted by a second run writing zero.

## Verification outcome

Five fresh-context verifier passes, all five refuting, twenty-one defects fixed.

**Fifth pass** showed `replaces_earlier` was unusable on its own. A move's new time is by
construction near where it is GOING, and the matcher ranked by nearness to that — so it
aimed at the destination and repainted whichever unrelated plan already sat closest to it,
leaving the plan that actually moved stale and destroying a bystander with no duplicate to
make the loss visible. `replaces_start_at` (v6) names the old time, exactly as
`resolves_what` names the commitment being resolved, and the match and the ranking are
both aimed at that instead. A move the message cannot aim — no stated origin — becomes a
new plan rather than an overwrite of the wrong one. Also fixed: day-precision rows all
scored as equally near, so re-reading a message that produced a Friday plan and a Saturday
plan could swap them and write on the second pass.

**Fourth pass** found four. Three shared a root cause I had now been wrong about in both
directions three times: matching picked a nearest row but never *rejected* one, so a 4pm
plan landed on an unrelated 9am row and destroyed it — while the previous round's
exact-time rule had duplicated every reschedule instead. Four rounds is enough evidence
that the times cannot answer the question, so the model is asked: `replaces_earlier` (v5,
mirroring `resolves` on commitments) says whether a message MOVES an existing plan.
Without it the answer is "different plan", because a duplicate is visible and dismissible
while a wrongly merged plan is silent data loss. A message a row already cites is treated
as a re-read whatever its time says, so re-extraction after a reschedule does not
duplicate. The fourth defect: `newest_citation_before` took a SQL `MAX()` over
offset-bearing timestamps, which is a string comparison — across the owner's two zones it
named the wrong citation and let a stale message repaint a corrected time.

This also removed the cross-day-reschedule limit that was documented as unfixable: a move
may cross a day boundary precisely because the message says it is a move, so a weekly
standing arrangement is still two plans.

**Third pass** found three, all one root cause: a dedup hit returned without recording the
row it claimed, so a second candidate in the same response could land on it too — the
match-then-match path no test had ever taken. It destroyed one plan outright ("coffee
Friday 9am" then "9am or 4pm?" wrote no 4pm row and repainted the 9am one), collapsed
same-day pairs on re-extraction, and — independently — the scan took the first agreeing
row in id order, so settling a second option repainted the first one instead. Matched rows
now join the same-response set, and the scan ranks agreeing rows by nearest start time.
The re-extraction thrash it flagged as minor is fixed too: a known time is only moved by a
message at least as recent as the newest already cited.

**Second pass** found five more, three of them defects in the first pass's *repairs* —
including one that was worse than the bug it replaced: separating plans on the clock
turned every reschedule ("push dinner to 7:30") into a duplicate row that double-booked
the day. Same-response matching now compares the clock, cross-message matching compares
the day and repaints the time. The declined-visibility fix had turned a resurrection bug
into a sink that swallowed real re-invitations; a declined row now matches only a message
it already cites. The review queue reached the brief but not the dashboard panel; plans
are in both now, with Accept and "Not a plan", and both queues are bounded below. The
person page rendered a 0.30-confidence plan as fact.

**First pass** found six, all reproduced and fixed with a regression test each:

1. Merging two people 500'd — migration 0014's NOT NULL foreign key to `entity` was never
   repointed by `merge()`. Fixing it surfaced two references that had never been repointed
   either, both older than this work: `activity.contact_entity_id` and
   `entity_merge.winner_id` (which broke the second merge of any cleanup pass). The
   invariant is now derived from the live schema, so the next table to forget fails a test.
2. A low-confidence plan was deleting real capacity from the day while the brief correctly
   hid it — rule 2 on one surface only.
3. Low-confidence plans reached no review queue at all; the count was reported and the
   entry did not exist.
4. A declined plan was invisible to dedup, so re-extraction (which the v4 prompt bump
   forces) resurrected cancelled plans as fresh proposals.
5. Dedup compared the day, not the hour, so two plans on one day collapsed and the second
   time was lost.
6. The brief had no lower bound, and nothing can move a plan to `done`, so plans stayed in
   it forever.

The lesson worth keeping: a green suite plus self-written revert-to-red proofs confirms
the path you built and says nothing about the paths you did not think of. See
`tasks/lessons.md` for the two recurring shapes (a new foreign key breaking existing
deleters; a filter added to one reader of a new column).

Still open from that pass: **`done` is unreachable.** Migration 0014 advertises the state,
`ADVANCES_TO` cannot get there, and there is no command or route. The brief no longer
grows without bound, and the person page files past plans under Earlier, so nothing is
broken — but a plan the owner actually attended cannot be marked as such. That wants a
small write surface, which is a design call rather than a fix.

## Known limits, found while building

- **An evening plan never appears on the day plan.** `capacity.compute` filters fixed
  events to the working window (default 09:00–18:00), so a 19:00 dinner is stored, is
  correctly excluded from work capacity, and then shows up on no schedule surface. It
  does reach the brief's Plans section, so it is not invisible — but most social plans
  are evenings, and the day view is where someone would look. Fixing it means deciding
  what the schedule page is: the working window, or the day. That is a design call, not
  a bug fix, so it is written down rather than guessed at.

  **Fixed 2026-08-05, and the design call turned out to be already made.** The cause was
  only half of what is written above: the Schedule page never read engagements *at all*
  — `day_view` called `capacity.fixed_events` (calendar only), so a confirmed 14:00
  coffee was just as invisible as a 19:00 dinner until the planner had run and left a
  plan_block behind. And the page had already answered the question: its ruler "always
  spans at least the default working window, widened to fit anything scheduled outside
  it" (`_window`), which is the day, not the window. So there was nothing to decide —
  one reader, `capacity.day_events`, now answers "what is immovable on this day" for
  both callers, and `compute` keeps the window filter, because *its* question really is
  how much work fits between nine and six.
- **Nothing yet exercises this against real messages.** Every test drives canned model
  responses. Whether the model reliably tells a commitment from an engagement is an eval
  question, and `evals/` is where it belongs — it never gates CI (docs/10).
- **A reschedule the model does not flag becomes a second plan.** `replaces_earlier` is
  the only thing that distinguishes "push dinner to Saturday" from "also dinner Saturday",
  so a missed flag duplicates. That is the deliberate direction: the duplicate is visible
  on the board and in the brief, whereas merging two plans that were not the same one
  silently deletes something the owner agreed to. Whether the model sets the flag reliably
  is an eval question, not a test one.
- **Legacy commitments that are really plans still double up.** Confirmed on the live
  ledger: six of the owner's commitments share a source item with one of the five
  engagements re-extraction produced, because everything read before the v4 prompt could
  only be filed as a commitment. `_already_a_commitment` suppresses the plan when the two
  are worded alike, but "Move-in: Willow Hall 502, 8:00am (regular move-in — Early Start
  early arrival declined)" and "ASU dorm move-in" score below the dedup threshold, so
  both survive. Widening the rule to suppress on the shared source item alone would eat
  the honest case where one message carries a commitment *and* a separate plan. The right
  fix is a decision about the four stale rows, not a looser rule — and dropping the
  owner's open commitments is not something to do unasked.

## Still dark, and blocking the rest

The build is done; the sources are not connected. In value order:

1. **Gmail** — `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`, then `backglass auth`. Phase 1
   of the project's own build order, never completed. Most invitations arrive here.
2. **iMessage** — Full Disk Access for both the terminal and the `uv` binary. 101 MB of
   real history is sitting there unread; `backglass setup` now says so honestly.
3. **Instagram / Slack** — a Meta export and a user token respectively. See
   `docs/07-connectors.md` §Turning on the messaging sources.

## Out of scope, deliberately

Writing back to anyone's calendar, sending replies, and any connector for a platform whose
terms forbid it. The owner is told what a plan is; the owner answers it.

---

# Evidence plumbing — sentence-level provenance, end to end

Started 2026-08-02. Fixes the three provenance defects found in the 2026-08-02 survey.
The OSS release plan that used to be this file is finished and follows below.

## Why

1. `extract/schemas.py:63` — the model returns the exact source sentence as
   `ExtractedCommitment.evidence`, described in that file as "provenance at the sentence
   level". `ledger.insert_commitment` never stored it. The review queue then rendered the
   *email subject* under a comment quoting docs/11 §4 "the exact source sentence beneath
   it in quotes" (`web/templates/_review.html:17`).
2. `brief/model.py:65` — every non-Gmail brief line links to `{base}/source/{external_id}`
   and no such route exists. The live ledger is 100% non-Gmail today (calendar:asu 200,
   apple-notes 65, anki 64, reminders 23, manual 13, avorio 3), so every provenance link
   in the brief is dead. Rule 1 says a claim with no provenance does not ship.
3. One commitment cites exactly one source_item. A restatement is silently discarded by
   the dedup step (`extract/commitments.py:112-117`), so the second and third sighting of
   an obligation leave no trace — and when dedup misses, the ledger grows a near-duplicate
   instead (live: ids 20/28, 21/29, 22/30 are the same three obligations twice).

## Steps

- [x] 0. Commit the tree's finished user_id/schema-reference work as a baseline.
- [x] 1. Migration `0013_commitment_evidence.sql`: `commitment_evidence`
      (user_id, commitment_id, source_item_id, quote, kind, seen_at, UNIQUE per pair),
      backfilled with one `original` row per existing commitment so every commitment has
      at least one citation. Add to FROZEN_CHECKSUMS, regenerate `specs/schema.sql`.
- [x] 2. `Ledger.record_evidence()` — idempotent (ON CONFLICT DO NOTHING, counts a write
      only when a row actually lands, so rule 3 still holds on a second run).
      `insert_commitment(evidence=…, evidence_kind=…)` writes the `original` row.
- [x] 3. `extract/commitments.py`: `_is_duplicate` → `_duplicate_of` returning the matched
      id; a dedup hit now records a `restated` citation on the commitment it matched
      instead of dropping the sentence. Quick-add (`web/actions.py:375`) records the
      owner's own words as a `manual` citation — the second door, per the 2026-08-02
      lesson about guards written at one call site.
- [x] 4. Queries carry the quote and the mention count: `open_commitments`,
      `dashboard_board`, `brief_needs_review`; brief queries gain `c.source_item_id` so a
      SourceRef can point at a real page.
- [x] 5. `GET /source/{id}` — the raw item, its triage verdict and reason, and everything
      derived from it (commitments, facts). `SourceRef.url()` and `_macros.source_link`
      point at it. Gmail keeps its external deep-link alongside.
- [x] 6. Tests: quote stored and rendered (drive the real door, not the function),
      restatement recorded without a second commitment, second run writes zero, brief
      provenance URL resolves against the app's real route table, unknown id 404s.

## Verification

`uv run pytest` green, ruff + mypy clean, and the review panel rendered from the demo db
shows a model sentence rather than a subject line.

---

# Backglass open-source release plan

Written 2026-08-01. Old todo.md (finished personal build log) archived to
`tasks/todo-archive-2026-08-01.md`. Revised after a fresh-context `plan-verifier` pass
found real gaps in the first draft (missing extraction-prompt files, an
under-specified medical.md handling, a missed PII class) — those are folded in below,
not left as follow-up.

## Goal

A fresh public GitHub repo (`contactdharsan-blip/backglass`) containing a single clean
commit of Backglass, MIT-licensed, BYOK-onboardable by a stranger with no context —
while the current private repo (renamed `backglass-private`) keeps its full history and
never gets pushed, cloned-with-history, or otherwise exposed. Done = a fresh `git clone`
of the **pushed public repo, in a directory outside the private checkout**, followed
only by the README/GETTING_STARTED steps, produces a working local dashboard, and an
independent grep of the pushed tree for every personal identifier returns zero hits.

## Decisions locked before execution

These are the forks explicitly delegated to this session (owner said "your call") —
recorded here so dk-executor doesn't re-derive them, not because they're still open:

- **Fictional persona:** Alex Rivera. `alex.rivera@example.com` (personal),
  `arivera@example.edu` (secondary/university — mirrors the real two-address
  `OWNER_EMAILS` shape). GitHub-handle-style fixture login: `alexrivera`.
  `example.com`/`example.edu` are conventionally-safe fake domains; never real domains.
- **Company/label namespace:** none needed — `com.cognifer.backglass.*` becomes
  `com.backglass.*`, which removes the company name entirely rather than replacing it.
- **Font:** Oswald (Bold/700) — OFL, on Google Fonts, condensed uppercase grotesque,
  explicitly in the owner's candidate list, closest match to "heavy condensed grotesque
  masthead" without introducing an unlisted face. Confirm visually against the current
  Mortend rendering before locking in; fall back to Archivo Black if the condensed
  proportions don't read right at masthead size.
- **Public repo naming (item F):** rename the current private repo to
  `backglass-private`; create a *new* repo named `backglass` (clean name) for the public
  push. Rationale: the canonical short name goes to the thing anyone can clone, the
  contaminated-history repo gets a name that signals "don't publish this," and there is
  no risk of ever needing to force-push over real history.
- **`specs/roadmaps/medical.md` handling:** ship a newly-authored, fully fictional
  replacement at the same path in the public export, rather than denying the whole
  medical/AMCAS feature (see B11 — this is real authoring work the first draft of this
  plan incorrectly assumed was unnecessary; see repo-facts note below for why).

**ANSWERED by owner 2026-08-02:** MIT copyright line = `Copyright (c) 2026 Dharsan
Kesavan` (owner explicitly chose the legal name). Consequence for C2: the scrub gate's
`Kesavan`/`Dharsan` bans must carry a single narrow exemption — the `LICENSE` file's
copyright line only (match `^Copyright \(c\) \d{4} Dharsan Kesavan$` in `LICENSE`,
nothing else, no other file). Any other hit anywhere, including elsewhere in LICENSE,
still fails the gate.

**B4 caveat resolved 2026-08-02:** owner's real `.env` did NOT set `MODEL_BACKEND`
(and has no API key) — the code-default flip would have silently broken the owner's
sync. `MODEL_BACKEND=claude_cli` is now pinned in the private `.env` (line 16), so the
`config.py:143` flip to `"anthropic"` is inert on this machine. Proceed with B4.

**Flag for the owner, not a plan blocker:** the current committed `specs/roadmaps/
medical.md` was re-read during planning and, as written, contains only relative week
offsets (`"week 208"`, `"22 weeks before the sit"`) and generic AAMC-cycle research
citations — no absolute dates, no names, no ASU references were found in the file
itself. The real MCAT/match-date specificity the owner is protecting likely lives in
the owner's *instantiated* roadmap rows (the private database) and in `docs/14-med-
student-prd.md`, not in this template file. This plan still treats `medical.md` as
private per the owner's explicit instruction — flagging the discrepancy rather than
silently overriding a stated privacy call either way.

## Repo facts this plan hinges on (verified 2026-08-01, re-verified after plan-verifier pass)

- `data/`, `.env`, `.claude/` already gitignored — no export-time scrubbing needed there.
- `tasks/` (todo.md, lessons.md, plan.md, audit-2026-08-01.md), `PROMPT.md`, `docs/13`,
  `docs/14`, `specs/roadmaps/medical.md` are all **tracked** and must be denylisted.
- `specs/roadmaps/{app-launch,founder,pm,swe,ship-blocked-product,company-revenue}.md`
  are **already generic/fictional** roadmap presets (no owner identifiers) — ship as-is,
  reference from GETTING_STARTED, no authoring needed for these six. `medical.md` is the
  one exception and **does** need new fictional content authored (B11) — it is not just
  a straight deny, because ~56 references across 5 test files structurally depend on a
  roadmap at that id existing and behaving a specific way (see B11 for the exact
  constraints).
- `desktop/` is already clean (`identifier: "com.backglass.desktop"`, no personal
  strings) — ships as-is, no genericization step needed.
- Checked-in `launchd/*.plist` hardcode `/Users/Dharsan/Downloads/backglass` and
  `/Users/Dharsan/.local/bin/uv` as absolute paths (not just the `com.cognifer.*`
  label) — they cannot ship as static files regardless of the rename. This is the real
  reason for the `schedule install` command, not just cosmetic label cleanup.
  `backglass/__main__.py:1646-1651` has a `LAUNCHD_LABELS` tuple checked by `doctor()`
  that must be updated in lockstep with the plist rename; `tests/test_doctor.py:23,33,
  39-40` asserts against the current `com.cognifer.backglass.*` labels and must be
  updated in the same change, not `tests/test_web_pages.py` (corrected from the first
  draft — verified `doctor()`'s launchd check is tested in `test_doctor.py`).
- No LICENSE file exists yet. No CI config, no `.python-version`, no `ruff.toml`, no
  `pytest.ini` exist — all tool config lives inside `pyproject.toml`
  (`[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]`), so `pyproject.toml` +
  `uv.lock` alone are sufficient in the export manifest for those.
- `pyproject.toml` project name is already `backglass` (no rename needed); it has no
  `license` field yet — add one alongside LICENSE (B8).
- `backglass/config.py:143` — `model_backend` field **defaults to `"claude_cli"` in
  code**, not just in `.env.example`. `.env.example`-only genericization (B4) is not
  enough: a stranger who unsets/never-sets `MODEL_BACKEND` still gets `claude_cli`,
  which needs the Claude Code CLI installed — not what a BYOK stranger has. This code
  default must change too (folded into B4 below), with an explicit caveat since it's a
  live behavior change to the private tree as well.
- `backglass/extract/prompts.py` loads `specs/extraction-prompts/*.md` **at runtime**
  (6 files: `extract-commitments.md`, `extract-goal-signal.md`, `roadmap-interview-
  adjust.md`, `roadmap-interview-questions.md`, `triage-batch.md`, `triage.md`) via
  `REPO_ROOT` in `backglass/config.py`. The first draft's export manifest omitted this
  directory entirely — without it, extraction is broken in the exported app. Corrected
  in C1 below.
- `docs/04-daily-schedule-and-goals.md` and `docs/07-connectors.md` were listed in
  Phase B's genericization scope but were missing from the first draft's export
  ALLOW_PATHS (an internal inconsistency — B2 genericizes them "for release" but they
  never shipped). Corrected in C1.
- Family-name strings `McKenna`/`Nyasha`/`Sheppard` (from the owner's banned list) have
  **zero current hits** in tracked files; `Gathas` (as "Shawn Gathas") appears in
  `tests/test_format_audit.py:170,180,217` as a fixture person name. Include all four in
  the scrub gate regardless (defense in depth for untracked/future content), and replace
  `Shawn Gathas` with a fictional fixture name in Phase B.
- **A missed PII class, found only by re-verification, not by name/email grepping:**
  `tests/test_github.py` uses the bare surname `kesavan` (no "Dharsan", not an email —
  a GitHub-username-shaped fixture) as a fake login/repo-owner throughout — 17 hits
  (lines 62, 64, 75, 120-121, 124, 147, 171, 255-260, 282, 300-301, 377, 432), e.g.
  `"repository_url": "https://api.github.com/repos/kesavan/backglass"`. This is exactly
  the failure mode the scrub gate exists to catch as a second net — a full-name/email
  grep alone missed it. `tests/test_facts.py` also has a bare "kesavan" hit outside the
  full-name occurrences already tracked. Folded into B1's scope and C2's banned list.

## Phase 0 — Precondition: commit current private-tree work

**Why first:** the export mechanism (Phase C) copies from a tree state; it must start
from a known, committed baseline, not ~50 modified + 3 untracked files.

**Steps** (judgment — grouping commits, otherwise mechanical `git add`/`git commit`):
1. Review `git status`/`git diff --stat` (already known: security middleware, local-day
   boundary fixes, dashboard work, format-audit work — the tail of recent commit
   history). Group into 2-4 logically-scoped commits if the split is obvious from the
   file list (e.g. "security middleware" / "local-day-boundary + dashboard" / "tests").
   If grouping isn't obvious in five minutes, one commit is an acceptable fallback —
   the goal is a known tree, not a clean history rewrite.
2. Commit with the owner's git identity (`contactdharsan@gmail.com` / repo default —
   do not override per global git conventions).

**Verification:** `git status` clean; `uv run pytest` green on the private tree before
moving to Phase B.

**Scope:** ~53 files, 0 new files created by this phase.

---

## Phase B — Scrub and genericize (in the private tree)

Every sub-step here is permanent hygiene to the private repo regardless of the release
— not release-only scaffolding. Order matters only where noted.

### B1. Bulk PII replacement in tests/fixtures — **mech-batch**

Exact string replacements, case-sensitive except where noted, across these **16**
files (expanded from the first draft's 13 after re-verification):
`tests/conftest.py`, `tests/test_idempotency.py`, `tests/test_gmail.py`,
`tests/test_noise.py`, `tests/test_triage_batch.py`, `tests/test_templates.py`,
`tests/test_boundary.py`, `tests/test_facts.py`, `tests/test_config.py`,
`tests/test_rules_and_entities.py`, `tests/test_batch.py`, `tests/test_connectors.py`,
`tests/test_format_audit.py`, **`tests/test_github.py`** (new — 17 `kesavan` hits),
**`tests/test_doctor.py`** (new — `com.cognifer.*` label fixtures, covered by B5 but
listed here too since it also carries the PII-adjacent label string), and **the 7
JSON fixtures under `tests/fixtures/commitments/`** (new — `01` through `07`, at least
`07-owner-in-cc.json:6,8` confirmed to carry `contactdharsan@gmail.com` /
`dkesava2@asu.edu`; check all 7 for the same pair).

| Old | New |
|---|---|
| `contactdharsan@gmail.com` | `alex.rivera@example.com` |
| `dkesava2@asu.edu` (any case, incl. `DKesava2@ASU.EDU`) | `arivera@example.edu` |
| `Dharsan Kesavan` | `Alex Rivera` |
| `Dharsan` (as a bare first-name reference, e.g. `test_config.py:57`'s
  `settings.owns("Dharsan <...>")`) | `Alex` |
| `kesavan` (bare, word-bounded, case-insensitive — the GitHub-fixture-username class
  found in `test_github.py`/`test_facts.py`) | `alexrivera` |
| `Shawn Gathas` (`test_format_audit.py:170,180,217`) | `Jordan Blake` (keep the
  "Guidance Coordinator" role string unchanged — only the name is PII) |

Do **not** touch `tasks/plan.md` or `tasks/todo.md`/`todo-archive` — they're
permanently denylisted from export (Phase C), scrubbing them is not required for
release safety and is out of scope here.

**Verification:** `grep -rniE "contactdharsan|dkesava2|\bdharsan\b|\bkesavan\b|shawn
gathas" tests/` returns nothing; `uv run pytest tests/` green (assertions reference the
new persona consistently — e.g. `test_config.py`'s `owns()` checks and
`test_github.py`'s repo-path assertions must use the new identifiers on both sides).

### B2. Doc genericization — **judgment**, 5 files

- `CLAUDE.md:14` — `"Single user. Owner is K, a founder who works across Arizona and
  India."` → drop the identity-revealing framing (name, founder role, specific
  geography). Keep "single user" and "runs entirely locally" as the load-bearing facts.
- `docs/04-daily-schedule-and-goals.md:101-108` — Phoenix/Kolkata/Coimbatore example
  under "Timezone and travel" (P13-P16). **Keep the timezone-pair mechanics and the
  worked example** (P16's "09:00 Phoenix call is 21:30 in Coimbatore" is illustrating
  real functional behavior the tests cover) — just reframe from "the owner" to a
  generic "you" / "a user who splits time between two zones," removing any phrasing
  that reads as this being one specific person's real travel pattern. **This file must
  end up in the shipped tree** (see C1 fix — it was missing from the first draft's
  manifest despite being genericized here).
- `docs/07-connectors.md:58-64` — Instagram allowlist section; already fairly generic
  ("the owner reads the handful of threads..."), light pass only if ASU-specific
  wording is found on closer read. **This file also ships** (C1 fix) and additionally
  needs new content: see B10's OAuth-setup requirement, which belongs here.
- `docs/10-tech-stack.md:88-100` — `claude_cli` rationale ("the owner has a
  subscription and no API key..."). Keep the reasoning (billing vs. engineering
  tradeoff is genuinely useful), reword "the owner" → "you" throughout so it reads as
  general advice, not personal narration.
- `docs/12-source-extraction-research.md:78` — `"Build the fixture set from K's real
  thread shapes"` → `"Build the fixture set from your own real thread shapes"` or
  equivalent; `K` is a bare-initial identity reference, must go.

**Verification:** `grep -rn "\bK's\|Owner is K\|Arizona and India\|Coimbatore.*owner\b"
docs/ CLAUDE.md` — manual read of each diff to confirm no functional content (timezone
semantics, connector allowlist rules, billing tradeoffs) was lost, only identity
framing.

### B3. `backglass/extract/dates.py:17-18` example decoupling — **judgment**, 1 file

Phoenix/Coimbatore appear as docstring/comment examples for relative-date resolution.
**Do not touch the Phoenix↔Kolkata boundary test semantics** (real functional tests
depend on this timezone pair) — only reword any comment/docstring prose that frames the
example as "the owner's" cities into a generic illustrative example using the same
timezone pair (the pair itself is fine to keep; it's genuinely useful as a real
UTC-7/UTC+5:30 example).

**Verification:** `uv run pytest tests/ -k timezone or -k local_day` green
(`tests/test_local_day_boundaries.py`, `tests/test_local_exposure.py` cover this).

### B4. `.env.example` cleanup + code default flip — **mech-batch**

- `MODEL_BACKEND=claude_cli` → `MODEL_BACKEND=anthropic` in `.env.example`. Reorder the
  comment block above it so `anthropic` (BYOK, the public default) is described first;
  keep `claude_cli` documented as the zero-marginal-cost option for people with a
  Claude subscription, and `deepinfra` as the third option. Don't remove any of the
  three.
- **`backglass/config.py:143`** — change the `model_backend` field's *code* default
  from `"claude_cli"` to `"anthropic"` so behavior matches the documented public
  default even when `MODEL_BACKEND` is unset, not just when `.env.example` is copied
  verbatim. **Caveat, don't skip this check:** this is a shared default for the private
  tree too — before flipping it, confirm the owner's real (gitignored) `.env` explicitly
  sets `MODEL_BACKEND` (near-certain given the rest of the env-driven design, but
  verify rather than assume; if it turns out unset, surface that to the owner before
  changing the code default out from under their running setup).
- `GMAIL_ACCOUNTS=personal,asu` → `GMAIL_ACCOUNTS=` (blank, template default) with the
  inline comment's example changed from `asu` to a generic label like `personal,school`
  or `personal,work`.
- `DEFAULT_TZ=America/Phoenix` / `ALT_TZ=Asia/Kolkata` → `DEFAULT_TZ=America/New_York`,
  `ALT_TZ=` blank. Neutral default a first-time cloner can immediately relate to;
  Phoenix/Kolkata as a *pair* stays alive in docs/tests as the worked example, just not
  baked into the template every new user copies.

**Verification:** `cp .env.example .env.test-scratch && diff` review; no functional env
var renamed or removed, only defaults/examples changed; `uv run pytest tests/test_config.py`
green after the code-default flip (check for any test asserting the old
`"claude_cli"` default and update it to `"anthropic"` in the same change).

### B5. `com.cognifer.*` → `com.backglass.*` rename — **mech-batch, coordinated**

Applies to 7 files plus one code constant, in one change (they must land together or
`doctor()` breaks against both the plists and its own tests):
- `launchd/com.cognifer.backglass.{sync,brief,plan,shutdown,batch-submit,batch-collect}.plist`
  → rename label+filename to `com.backglass.{sync,brief,plan,shutdown,batch-submit,
  batch-collect}.plist` (drop the doubled "backglass.backglass" — the label should read
  `com.backglass.sync` etc., matching `LAUNCHD_LABELS` below and consistent with the
  desktop app's existing `com.backglass.desktop` identifier).
- Inside each plist: `<key>Label</key><string>com.cognifer.backglass.sync</string>` →
  `com.backglass.sync`, and replace the hardcoded `/Users/Dharsan/Downloads/backglass`
  and `/Users/Dharsan/.local/bin/uv` paths with placeholder tokens (`{{REPO_DIR}}`,
  `{{UV_BIN}}`, `{{HOME}}`) — these become templates, not directly-installable files
  (see B6; move them to `launchd/templates/*.plist.tmpl`).
- `backglass/__main__.py:1646-1651` — `LAUNCHD_LABELS` tuple: `com.cognifer.backglass.*`
  → `com.backglass.*`.
- **`tests/test_doctor.py:23,33,39-40`** — update the `PARTIAL`/expected-label fixtures
  from `com.cognifer.backglass.*` to `com.backglass.*` (corrected target file from the
  first draft, which pointed at `test_web_pages.py`).
- `launchd/README.md` — update the label examples and the "Install" section to
  reference `backglass schedule install` (B6) as the primary path; keep the manual
  `cp`/`launchctl load` steps as a documented fallback but pointed at the template dir.

**Verification:** `grep -rn "cognifer" .` (whole tracked tree) returns zero hits;
`uv run pytest tests/test_doctor.py` green with the new labels.

### B6. `backglass schedule install` command — **judgment, new small feature**

New module (e.g. `backglass/schedule.py`) + CLI subcommand under `__main__.py`:
- `render(repo_dir: Path, uv_bin: str, home: Path) -> dict[str, str]` — reads
  `launchd/templates/*.plist.tmpl`, substitutes `{{REPO_DIR}}`/`{{UV_BIN}}`/`{{HOME}}`
  resolved via `Path(__file__).resolve().parents[1]` (or cwd — pick whichever the repo
  already resolves paths by elsewhere for consistency, e.g. `backglass/config.py`'s
  `REPO_ROOT`), `shutil.which("uv")`, `Path.home()`.
- `backglass schedule install [--dry-run]` — renders all templates, writes them to
  `~/Library/LaunchAgents/`, runs `launchctl load` on each (skip in `--dry-run`, just
  print the rendered plists).
- `backglass schedule status` (optional, nice-to-have, skip if it duplicates
  `doctor()`'s existing launchd check too closely — judgment call, don't add pure
  redundancy).

New test file `tests/test_schedule.py`: template rendering produces valid XML with no
literal `{{...}}` left, resolves to the right paths given a fake repo dir / fake `which
uv` / fake home; `--dry-run` writes nothing.

**Verification:** `uv run pytest tests/test_schedule.py` green; manual dry-run against
the current machine (`uv run backglass schedule install --dry-run`) produces plists
with real absolute paths and no `{{` left in the output.

**Scope:** ~4 new files (`backglass/schedule.py`, `tests/test_schedule.py`, 6
`launchd/templates/*.plist.tmpl` replacing the 6 old `.plist` files), edits to
`__main__.py` and `launchd/README.md`.

### B7. Font replacement — **judgment**, 3-4 files

- Fetch Oswald Bold (OFL) from Google Fonts, convert to woff2 (same fontTools pipeline
  used for Mortend, per `VENDOR.md`'s existing recipe), place at
  `backglass/web/static/fonts/Oswald-Bold.woff2`, delete `Mortend-Bold.woff2`.
- Update `backglass/web/static/VENDOR.md`: replace the Mortend entry with Oswald's
  source URL, sha256, byte count, and `license OFL 1.1 — permissive, ships freely`.
- Update `design/tokens.css:88` (`--font-cond`) and
  `backglass/web/static/dashboard.css:23-31` (`@font-face` declaration,
  `font-family:"Mortend"` references) to `"Oswald"`.
- Visual check: launch the dashboard against `data/backglass-demo.db` (never the real
  `backglass.db` — see `tasks/lessons.md`'s 2026-08-01 lesson on live-db browser
  testing) and screenshot the masthead before/after to confirm the condensed-uppercase
  character survives the swap. If it reads noticeably lighter/wider than Mortend, fall
  back to Archivo Black per the locked-decision note above.

**Verification:** `uv run pytest tests/test_dashboard.py -k font` (if such a test
exists — check) or a manual visual diff; `VENDOR.md` license line says OFL, not
Freeware Non-Commercial; no remaining `Mortend` string anywhere in tracked files.

### B8. LICENSE file — **mechanical, blocked on owner's copyright-name answer**

Standard MIT text, `Copyright (c) 2026 <owner's chosen name/handle>`.

**Verification:** file present at repo root; `pyproject.toml` gets a `license = "MIT"`
field (confirmed none exists yet).

### B9. README.md rewrite — **judgment**

Current README (57 lines) describes the product well but has a stale "Getting started"
(`python -m backglass.db init` — wrong, the real CLI is `backglass init`/`setup`/etc.)
and a stale "Status: Pre-phase-0. Nothing is built." Rewrite:
- Keep the product framing (the pinball backglass metaphor, the "not a search tool"
  thesis) — it's good and accurate.
- Replace "Getting started" with a short pointer to `GETTING_STARTED.md` (B10) rather
  than duplicating steps inline.
- Replace "Status: Pre-phase-0" with an accurate current-state line.
- Update "Layout" table: drop `PROMPT.md` (not shipped) from the public layout
  description.
- Add a "License" line (MIT) and a one-line BYOK statement near the top ("bring your
  own Anthropic/DeepInfra API key — no hosted service, everything runs on your
  machine").

### B10. GETTING_STARTED.md — **judgment**, new file, plus new OAuth-setup content

Exact sequence (verified against the real CLI, `backglass/__main__.py`):
```
git clone <repo> && cd backglass
uv sync                              # installs Python deps
cp .env.example .env                 # set ANTHROPIC_API_KEY (or MODEL_BACKEND=claude_cli/deepinfra)
uv run backglass init                # runs migrations
uv run backglass setup               # auto-detects local sources, writes remaining .env
uv run backglass auth gmail          # OAuth — needs your own Google Cloud OAuth client (see below)
uv run backglass sync
uv run backglass dashboard
uv run backglass schedule install    # optional: launchd jobs for automatic sync/brief
```
**Confirmed gap, not a hedge:** `docs/07-connectors.md` currently has **zero** mentions
of Google Cloud / OAuth client / `GOOGLE_CLIENT_ID` (`grep -in "google cloud|oauth
client|console.cloud" docs/07-connectors.md` returns nothing). This content does not
exist yet and must be authored, not linked to as if it already exists: a short
"Setting up Gmail/Calendar/Drive OAuth" section in `docs/07-connectors.md` covering
creating a Google Cloud project, enabling the Gmail/Calendar/Drive APIs, creating an
OAuth client (Desktop app type), and where `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` go
in `.env`. GETTING_STARTED.md links to this new section rather than duplicating it.

Reference the existing fictional roadmap presets (`specs/roadmaps/{founder,app-launch,
pm,swe,ship-blocked-product,company-revenue,medical}.md` — `medical` now fictional per
B11) as example starting points via `backglass roadmap paths` / `backglass roadmap
start <path>`.

**Verification (B9+B10):** a person with zero prior context can read only these two
files plus the new docs/07 section and correctly predict every command in Phase D's
fresh-clone smoke test before it runs.

### B11. Author a fictional `specs/roadmaps/medical.md` replacement for public export — **judgment, new authoring, ~1 file**

New — added after the plan-verifier pass caught that denying `medical.md` outright
(the first draft's approach) breaks the exported test suite: ~56 references across
`tests/test_web_pages.py`, `tests/test_goals_totals.py`, `tests/test_med_phase_a.py`,
`tests/test_roadmap.py`, `tests/test_interview.py` load the `"medical"` preset by id and
assert on its structural shape. This also directly satisfies the original release
brief's ask ("roadmap feature ships with fully fictional sample presets") for the one
preset that wasn't already generic.

**Where it lives:** author the fictional content at a new private-tree-only staging
path, `specs/roadmaps/medical.public.md` — the real `specs/roadmaps/medical.md` stays
untouched and denylisted (never read by the export). C1's build script substitutes the
staging file in at the real path only inside the assembled export tree (see
`SUBSTITUTIONS` in C1). This generalizes cleanly if any other file ever needs the same
treatment.

**Exact structural constraints the replacement must satisfy** (extracted from the
tests that couple to it — write to these, then run the full suite and treat any
remaining failure as this step's bug to fix, not a plan deviation):
- `id: medical`, `horizon: annual` (frontmatter shape matches the other six presets).
- `max(offset_weeks) >= 200` across all steps (`tests/test_roadmap.py::
  test_medical_spans_about_four_years`).
- Exactly 5 steps of `kind: total` whose category keys line up with 5 of
  `backglass/goals/activities.py`'s `CATEGORIES = ("shadowing", "clinical",
  "volunteering", "research", "leadership", "other")` — asserted via `SELECT COUNT(*)
  ... WHERE t.kind = 'total'` `== 5` in `tests/test_web_pages.py::
  test_masthead_spends_at_most_two_reels`.
- A step structure rich enough to support the interview-adjustment flow
  (`tests/test_interview.py` loads and adjusts the preset repeatedly) — mirror the
  existing `medical.md`'s step count/types rather than minimizing.
- Fictional content only: no AAMC/Shemmassian-style real citations required to keep
  (fine to keep genre-appropriate framing, e.g. "national matriculant medians" as
  illustrative, non-owner-specific text), no real names, no absolute dates — offsets
  only, same as the other six presets.

**Verification:** `uv run pytest tests/test_web_pages.py tests/test_goals_totals.py
tests/test_med_phase_a.py tests/test_roadmap.py tests/test_interview.py` green when run
against the *exported* tree (where the substitution has happened) — this file is never
tested against the private tree's real medical.md, since it never ships there.

---

## Phase B checkpoint — full suite green

`uv run pytest` full suite, private tree, after all of B1-B10 (B11's content is only
exercised inside the export tree — see C3/D). Do not proceed to Phase C on red tests.

---

## Phase C — Export mechanism (new scripts, private tree only — never shipped)

**Design choice: allowlist, not denylist, as the primary mechanism.** Considered
denylist-over-`git ls-files` (ship everything except a banned-paths list) — rejected
because it's fail-dangerous: a new personal file added to the repo six months from now
and never added to the denylist leaks into the public repo silently. An explicit
allowlist is fail-safe (a forgotten new file just doesn't ship, which is annoying, not
dangerous) and directly serves item H's stated goal — this *is* the boundary mechanism
future personal work has to respect. The string-grep gate (below) is a second,
independent net on top, not a replacement for the allowlist — and per the `kesavan`
finding above, that second net is not decorative: a name/email grep alone already
missed one real PII class in this exact repo.

### C1. `scripts/release/manifest.py` — **mechanical**, encodes the decided list

```python
ALLOW_PATHS = [
    "backglass/",                    # entire package (after B1-B8 edits)
    "tests/",                        # entire (after B1 scrub)
    "docs/01-product-brief.md", "docs/02-architecture.md", "docs/03-data-model.md",
    "docs/04-daily-schedule-and-goals.md",   # added — was genericized in B2 but
                                              # missing from the first draft's manifest
    "docs/05-morning-brief.md", "docs/06-dashboard.md",
    "docs/07-connectors.md",                 # added — same gap, plus new OAuth section
    "docs/08-privacy-and-data-boundary.md", "docs/09-build-plan.md",
    "docs/10-tech-stack.md", "docs/11-ux-flows.md",
    "docs/12-source-extraction-research.md",
    "design/",                       # entire
    "specs/schema.sql",
    "specs/extraction-prompts/",     # added — backglass/extract/prompts.py loads these
                                      # at runtime; omitting them breaks extraction
    "specs/roadmaps/",                # minus medical.md, see DENY + SUBSTITUTIONS
    "launchd/README.md", "launchd/templates/",
    "desktop/",                      # entire, already clean
    "scripts/release/",              # ships itself, so future public-repo forks
                                      # inherit the same export tooling — judgment call,
                                      # confirm no private-only assumptions leak in
    "scripts/_validator_core.js", "scripts/validate-palette.mjs",
    "README.md", "GETTING_STARTED.md", "LICENSE", "CLAUDE.md",
    "pyproject.toml", "uv.lock", ".gitignore", ".env.example",
]
DENY_PATHS = [   # subtracted even if under an ALLOW_PATHS prefix
    "tasks/", "PROMPT.md", "docs/13-activation-runbook.md",
    "docs/14-med-student-prd.md", "specs/roadmaps/medical.md",
    "data/", ".env", ".claude/",
]
SUBSTITUTIONS = {   # path in the export tree -> source path to copy from instead
    "specs/roadmaps/medical.md": "specs/roadmaps/medical.public.md",  # see B11
}
```
Confirm this list against a fresh `find backglass tests docs design specs launchd
desktop scripts -type f` at execution time once B is done — this revision already
closes the two gaps the plan-verifier found (`specs/extraction-prompts/`, `docs/04`
and `docs/07`), but re-verify rather than assume it's now exhaustive.

### C2. `scripts/release/scrub_gate.py` — **mechanical**

Greps an assembled tree for a banned-string list and exits non-zero with
`file:line:match` on any hit. Banned list, with explicit per-term matching rules since
naive case-insensitive substring matching produces false positives on short/common
terms (e.g. `ASU` inside "measure", "casual") and false negatives on missed classes
(the `kesavan` GitHub-fixture-username miss above):
- Word-bounded (`\bTERM\b`), case-insensitive: `ASU`, `Cognifer`, `Kesavan`,
  `McKenna`, `Gathas`, `Nyasha`, `Sheppard`.
- Substring, case-insensitive: `Dharsan`, `contactdharsan`, `dkesava2`, `kesavand`,
  `/Users/Dharsan`.
- Literal, case-sensitive: `Arizona State`.

Keep the list in a constant at the top of the file so it's trivially extendable later
without touching the walking logic.

### C3. `scripts/release/build_public_repo.py` — **mechanical orchestration**

1. Assemble: for each `ALLOW_PATHS` entry minus `DENY_PATHS`, copy from the private
   working tree (`git ls-files` filtered by the manifest, applying `SUBSTITUTIONS`
   before the deny check so `medical.md`'s replacement content lands at the real path)
   into a fresh scratch dir. **Must not use `git clone`/`git archive`/`git filter-repo`
   of the private repo** — a plain file copy into a directory with no `.git` at any
   point. The scratch dir must live outside the private checkout (or be independently
   gitignored) so a later `git add -A` in the private tree can never sweep it in.
2. Assert the scratch dir has no `.git` directory before step 4's `git init` — a cheap
   guard against a future edit accidentally reintroducing a clone-based copy step.
3. Run `scrub_gate.py <scratch-dir>` — abort on non-zero.
4. `cd <scratch-dir> && uv sync && uv run pytest` — abort on non-zero. This proves the
   exported tree is self-contained (no accidental import of something outside the
   allowlist) and exercises B11's fictional medical.md against the real test suite for
   the first time.
5. `git init && git add -A && git commit` with the message describing the release and
   the LICENSE author line from B8; assert `git rev-list --count HEAD == 1`
   immediately after. **Do not push** — that's a separate, explicit Phase E step so a
   human reviews the assembled tree first.

### C4. `tests/test_release_manifest.py` — **mechanical**, cheap and permanent

Asserts `DENY_PATHS` entries are never resolvable under any `ALLOW_PATHS` prefix (a
static assertion against the manifest data structure itself, not a filesystem walk) —
catches someone widening an ALLOW prefix in a way that silently un-denies
`docs/14-med-student-prd.md` etc. Also asserts every `SUBSTITUTIONS` source path exists
on disk (catches `medical.public.md` going stale/renamed silently). This is the
regression test for item H.

**Verification (Phase C as a whole):** `uv run pytest tests/test_release_manifest.py`
green; running `build_public_repo.py --dry-run` (list what would be copied, don't
write) against the current private tree prints a file list that a human eyeballs once
against the ALLOW/DENY/SUBSTITUTIONS intent above.

**Scope:** 4 new files (manifest, scrub gate, build script, test), 0 existing files
touched, plus B11's `medical.public.md`.

---

## Phase D — Run the export and verify the assembled tree

Judgment (interpreting failures), mechanical (running the scripts from Phase C).

1. `uv run python scripts/release/build_public_repo.py --out <scratch-dir>` (no
   `--dry-run` this time) — produces a committed tree at `<scratch-dir>`.
2. Independent double-check, **outside** the gate script (per item G's explicit ask):
   `grep -rniE "\bdharsan\b|\bkesavan\b|contactdharsan|dkesava2|kesavand|
   /users/dharsan|\bcognifer\b|\basu\b|arizona state|mckenna|gathas|nyasha|sheppard"
   <scratch-dir>` — must return nothing.
3. Fresh-clone smoke test, simulating a brand-new user with no `.env` and no `data/`,
   run inside `<scratch-dir>` (pre-push — a second post-push clone test happens in
   Phase E):
   - `uv run backglass init` — creates DB, runs migrations, no crash.
   - `uv run backglass setup` — auto-detect with nothing configured; should report
     "nothing found" gracefully, not throw.
   - `uv run backglass doctor` — reports missing API key / missing OAuth creds clearly,
     non-zero exit, no stack trace (per Rule 5: degrade, don't blow up).
   - `uv run backglass dashboard` — serves an empty-state page (no commitments) without
     erroring.
   - `uv run backglass sync` — fails gracefully with no model key set (clear error
     message about the missing key, exercising the new `anthropic` code default from
     B4), exits non-zero, does not crash mid-write.
4. `cd <scratch-dir> && uv run pytest` again post-copy (belt and suspenders on top of
   C3's in-script run, since this is the tree that's about to be pushed).

**Verification:** all four smoke-test commands behave as described above (this is the
actual "would a stranger survive first contact" check); grep in step 2 is empty;
pytest green.

**Risk flagged here, not glossed over:** a smoke test against an *empty* DB is not the
same as testing against a real one — if `setup`/`doctor`/`dashboard` have any code path
that assumes at least one row exists somewhere (a common bug class), this is where it
surfaces. If any of the four commands crash instead of degrading, that's a real bug to
fix before Phase E, not a plan deviation to push through.

---

## Phase E — GitHub mechanics

1. `gh repo rename backglass-private --repo contactdharsan-blip/backglass` (renames the
   current private repo; confirm via `gh repo view contactdharsan-blip/backglass-private
   --json visibility` → still PRIVATE).
2. Update the private working copy's remote: `git remote set-url origin
   https://github.com/contactdharsan-blip/backglass-private.git` — otherwise future
   pushes from the existing checkout fail against the renamed repo.
3. `gh repo create contactdharsan-blip/backglass --public --description "..."` (no
   `--source`/`--push` flag — push separately from the scratch dir so there's no risk
   of `gh` inferring the wrong source tree).
4. From `<scratch-dir>`: `git remote add origin
   https://github.com/contactdharsan-blip/backglass.git && git push -u origin main`
   (confirm default branch name matches what `gh repo create` picked, likely `main`).
5. Verify: `gh repo view contactdharsan-blip/backglass --json visibility,name` →
   `PUBLIC`, `backglass`; `git log` in the pushed repo shows exactly one commit; spot-
   check the GitHub web UI file tree against the C1 allowlist.
6. **Post-push clone smoke test (new — closes the gap the plan-verifier flagged: Phase
   D only tests the pre-push scratch tree, which is not proof the *pushed* repo works
   for a stranger).** In a directory fully outside the private checkout (e.g. a fresh
   scratchpad path), `git clone https://github.com/contactdharsan-blip/backglass.git`
   and run GETTING_STARTED.md's command sequence verbatim, with no shortcuts and no
   reference to any file from the private tree. This is the check that would have
   caught the missing `specs/extraction-prompts/` gap in the first draft.

**Verification:** both `gh repo view` calls above return the expected visibility;
single-commit history confirmed via `git log --oneline` against the remote; step 6's
fresh clone completes GETTING_STARTED's sequence through `backglass dashboard` serving.

---

## Phase F — Post-release hygiene (private tree, small doc change)

Add a short section to the private repo (e.g. append to `CLAUDE.md` or a new private-
only `tasks/release-process.md` — either is fine since `tasks/` is permanently
denylisted) documenting: the manifest (`scripts/release/manifest.py`) is the boundary
for what's public; personal presets/paths (`specs/roadmaps/medical.md`, `docs/14`,
`tasks/`, `PROMPT.md`, `docs/13`) must stay on the `DENY_PATHS` side of that boundary;
future public updates re-run `build_public_repo.py`, diff the new scratch tree against
the public repo's current single commit, and decide whether to squash into a new single
commit or start accumulating public history from here.

**Verification:** file exists, is itself covered by an existing DENY_PATHS entry (if
using `tasks/`) or manually confirmed not to be in ALLOW_PATHS (if elsewhere).

---

## Risks

- **Scrub-list incompleteness.** The banned-string list is necessarily finite; a name
  or detail not on it slips through both the gate and the manual grep. Concretely
  demonstrated during planning: an email/full-name grep alone missed the bare
  `kesavan` GitHub-fixture-username class in `tests/test_github.py` (17 hits) — only
  caught by a fresh-context adversarial pass. Mitigation: the allowlist-first design
  means most personal content never enters the export candidate set at all — the
  string gate only has to catch what leaked *into* an allowed file, a much smaller
  surface than "everything in the repo" — but treat the banned-string list as
  provisional, not complete, and re-grep broadly (not just the known terms) before
  Phase E's push.
- **History leakage via tooling, not just content.** If `build_public_repo.py` ever
  uses `git clone`/`git archive`/`git filter-repo` on the private repo instead of a
  plain file copy into a `.git`-less directory, contaminated history rides along even
  if file *contents* are clean. This is a hard constraint on C3's implementation, not
  a preference — filter-repo/history-rewriting approaches are explicitly the wrong
  tool here given "history is contaminated... never publish it."
- **`medical.md`'s substitution mechanism is new and only exercised once.** If B11's
  fictional replacement doesn't satisfy every structural coupling on the first attempt
  (5 total-kind steps, 200+ week span, interview-flow richness), the exported test
  suite fails in C3/D in a way the private tree's own test run never would (since the
  private tree always tests against the real medical.md). Budget iteration time here
  specifically, and don't treat a red exported-tree suite as a manifest bug before
  checking whether it's actually a content-shape mismatch in `medical.public.md`.
- **`doctor()`/`schedule install` behavior change is live in the private tree too.**
  Renaming `LAUNCHD_LABELS` and moving from static plists to rendered templates changes
  what the owner's *own* machine needs (existing `com.cognifer.*` launchd jobs must be
  unloaded and replaced with `com.backglass.*` ones via the new command) — this is a
  manual operational step for the owner after B5/B6 land, independent of the public
  release. Flag it explicitly when B5/B6 complete; don't assume it's automatic.
- **`model_backend`'s code-default flip (B4) is also a live behavior change** for
  whatever in the private tree relies on the implicit `claude_cli` default — verify the
  owner's real `.env` sets it explicitly before assuming this is inert there.
- **Font swap is a visual judgment call**, not a spec-checkable one. Requires an actual
  screenshot comparison, not just "OFL and condensed, ship it."
- **LICENSE copyright name** is identity-adjacent by nature — must come from the owner,
  not be inferred or defaulted.
- **Phase 0's grouped commit(s) may be imperfectly scoped** (mixing security middleware
  with unrelated dashboard fixes) — acceptable per explicit instruction, but don't let
  "known tree" become an excuse to skip a real `pytest` run before Phase B starts.

## Verification summary (definition of done)

1. Private tree: `uv run pytest` green after Phase 0 and again after Phase B.
2. `uv run pytest tests/test_release_manifest.py` green (Phase C's regression test).
3. `scripts/release/scrub_gate.py` exits 0 against the assembled export tree.
4. Independent manual grep (Phase D step 2), using the expanded term list above, of the
   assembled tree — zero hits.
5. `uv run pytest` green *inside* the exported/scratch tree using its own `uv sync`
   (proves no hidden dependency on the private tree, and exercises B11's fictional
   medical.md for the first time).
6. Fresh-clone smoke test (Phase D step 3, pre-push) — init/setup/doctor/dashboard/
   sync-without-keys all behave per Rule 5 (degrade, never crash).
7. `gh repo view contactdharsan-blip/backglass --json visibility,name` → `PUBLIC`,
   `backglass`; exactly one commit in its history.
8. `gh repo view contactdharsan-blip/backglass-private --json visibility` → `PRIVATE`,
   unchanged full history.
9. **Post-push clone smoke test (Phase E step 6)** — a completely fresh `git clone`
   outside the private checkout, run through GETTING_STARTED.md verbatim, reaches a
   serving dashboard. This is the actual "done" check the Goal states; steps 1-8 are
   necessary but not sufficient without this one.
10. LICENSE present with an owner-approved copyright name; MIT text unmodified.

---

# Motion layer: animate every action and every page change

Written 2026-08-07. Goal: "add animations for all actions and tab changes throughout
the site."

**Assumption, stated because nothing in the markup settles it:** there are no literal
tab controls in this app. "Tab changes" means the sidebar page navigation (nine full
document loads) plus the 1–9 keyboard page switch in `base.html`. Panel-swapping
HTMX targets are "actions", not tabs. Planned on that reading; no question asked.

## What is there now

One line of motion in the whole product: `.htmx-swapping{opacity:0;transition:.08s}`
at `dashboard.css:324` — and it never plays, because htmx 2.0.4's
`defaultSwapDelay` is `0`, so the outgoing node is replaced before a transition can
run. `defaultSettleDelay` is `20`. `design-system.md` has no motion section at all.

## Shape of the work

1. **Motion tokens** in `design/tokens.css` — durations and the three strong curves,
   with one `prefers-reduced-motion: reduce` block that zeroes the durations at the
   token layer instead of scattering overrides.
2. **One motion layer** appended to `dashboard.css`, single author, no per-page forks.
   Transform and opacity only (§8 rule 7 — no shadows, no gradients — extends to
   motion: things fade and slide, they never levitate).
3. **Swap animation with zero template edits** — `htmx.config.defaultSwapDelay` in
   `base.html` plus CSS on `.htmx-swapping` / `.htmx-added` / `.htmx-settling` covers
   all 94 `hx-` attributes at once. Every template edit avoided is a `panel_slice`
   test not broken.
4. **Page transitions** — `@view-transition { navigation: auto }` as progressive
   enhancement, probed in both Safari and the Tauri WKWebView rather than assumed,
   over an unconditional body fade-in baseline.
5. **§9 Motion** in `design-system.md`, with the frequency gate written down — this
   repo records its rulings there, so the section is deliverable, not garnish.

## Frequency gate (Kowalski), applied to this product

- Board Resolve/Snooze/Done and the 1–9 page keys fire tens of times a day: minimal
  or no motion. A daily action that got slower is a regression, not a polish pass.
- Panel swaps, the review queue, the failed-write strip: occasional — standard.
- Nothing here is rare enough to earn delight.

## Verification

- `uv run pytest` green (no template edits expected; prove it).
- `node scripts/validate-palette.mjs` after touching tokens.
- Adversarial CSS audit: transform/opacity only, every duration under 300ms, no
  `ease-in`, no keyframes on frequently-triggered elements, reduced-motion reaching
  every rule.
- Look at it in Safari, cache-busted, both themes (2026-08-06 lesson: a screenshot is
  a cache, not an observation).

## Motion: what the verifier caught

A fresh-context verifier refuted the first version of this. Three swap targets reached
no arrival rule — `#decisions`, `#memory` (both `.sec` sections) and `#week-grid`, the
region a checklist tick repaints out of band — and `.lnk`, which is the control the
whole roadmap surface is built from, was in no rule at all. The selector list had been
written from an id inventory, and an id inventory is a list that is correct on the day
it is written.

Fixed by selecting on `.panel`/`.sec` instead, and by adding the test that compares
every `hx-target` and `hx-swap-oob` region in the templates against the arrival rule.
Both the fix and the test were proven against a mutation: removing `.sec` lists five
regions by name.
