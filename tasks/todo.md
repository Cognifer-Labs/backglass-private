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

## Left for the palette's owner

Both are decisions, not defects, and both sit in the file another session is editing:

- Black on vermilion measures 4.40 in dark mode — on chips, alert text and card
  titles. §3's own table records 4.40 and ships it, but §3 also calls gold "the only
  such exception in the system", and this is a second one under the 4.5 floor.
- The week grid's event links are 22px tall with no gap to their neighbours. Height is
  duration there, so the size is arguably essential and exempt — but the neighbours
  part is real, and the fix is spacing the grid, not padding the link.

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

**Two syncs can run at once.** There is no lock: not a file lock, not a row, not a check
of the `run` table for an unfinished row. The launchd job fires every 30 minutes and a
manual `backglass sync` during a backfill will overlap it, at which point both processes
select the same `pending_extraction` rows and extract them twice. The ledger's
immutability trigger does not catch this, because two extractions of one item are two
legitimate-looking commitment inserts; the only thing standing between that and a
duplicated ledger is the 0.85 fuzzy dedup, which is a similarity heuristic and not a
guarantee. Worked around here by unloading the job. The fix is a `BEGIN IMMEDIATE`-held
row or an advisory lock file taken for the length of a run, and it belongs in its own
change with its own test.

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

Instagram still reads its env allowlist; the table is wired for it (`source` is already
per-service) but the connector does not yet report sightings. That is the obvious next
step and is deliberately not claimed here.

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
