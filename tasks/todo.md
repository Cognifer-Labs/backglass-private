# Build log — Phases 1–5 built

> Active session checklist. The full arc across all phases, and the AI cost
> architecture, is in [`tasks/plan.md`](plan.md). Phase 1 is written but not green —
> see plan.md §0 and §Phase 1A before picking anything up here.

**Goal.** Schema, an incremental Gmail connector with the docs/08 boundary hook enforced
before persistence, and two-tier extraction writing `commitment` rows. Commitments only —
no goals, no plans, no brief, no dashboard.

**Acceptance criterion** (docs/09 §Phase 1): a SQL query returns the owner's actual open
commitments, correctly, and a second run over unchanged input writes nothing.

**Decisions taken this session** (see §Deviations for the ones that contradict a doc):

| Decision | Value |
|---|---|
| Data boundary | Option B, `BOUNDARY_MODE=full_scope`. Neither ingested mailbox carries client correspondence. |
| Mailboxes | `contactdharsan@gmail.com` + `dkesava2@asu.edu`, both in Phase 1 |
| Owner identity | both addresses are "the owner" for `i_owe` vs `owed_to_me` |
| Model access | Claude Code CLI on subscription now; DeepInfra + open-source models on the product path |
| Spend cap | `MONTHLY_SPEND_CAP_CENTS=2000` from `.env.example`, kept as shadow accounting from the CLI's reported `total_cost_usd`, plus a hard per-call `--max-budget-usd` |

---

## Session 1 — scaffold and schema

- [x] `pyproject.toml` — uv project, `requires-python = ">=3.12"`, deps: typer,
      pydantic, pydantic-settings, google-api-python-client, google-auth-oauthlib;
      dev: pytest, ruff, mypy, vcrpy. Ruff + mypy (strict on `backglass/`) + pytest config.
- [x] `backglass/__init__.py`, `backglass/__main__.py` — Typer app with every command in
      docs/10 §CLI stubbed, `--help` complete
- [x] `backglass/config.py` — pydantic-settings over `.env`; `OWNER_EMAILS` as a list
- [x] `backglass/db/__init__.py` — `connect()` with `PRAGMA journal_mode=WAL`,
      `PRAGMA foreign_keys=ON`, dict row factory; `migrate()`; refuses to start on a
      file-set/recorded-version mismatch
- [x] `backglass/db/migrations/0001_initial.sql` — `specs/schema.sql` verbatim plus a
      `schema_version` table
- [x] `backglass/db/queries/` — `.sql` files loaded by a thin helper
- [x] `backglass init` fully working against a temp db
- [x] `tests/test_migrations.py` — init is idempotent; version mismatch refuses to start

## Session 2 — data boundary, then Gmail ingest

Boundary first, with its tests, before any network code exists.

- [x] `backglass/connectors/boundary.py` — docs/08 D1–D6. Domain + explicit-address match,
      case-insensitive, subdomains included, across From/To/Cc/Bcc. Returns a verdict the
      connector calls **before it yields**. Tally recorded on `run.items_excluded`.
- [x] `tests/test_boundary.py` — docs/08 **D7**, non-skippable: a message with a denylisted
      address in any recipient field produces zero rows. Runs under `BOUNDARY_MODE=exclude`
      with fake denylist domains regardless of the configured runtime mode.
- [x] `backglass purge-boundary` — D6, purges previously stored items matching a
      newly-added denylist entry and reports what was removed
- [x] `backglass/connectors/base.py` — the `Connector` Protocol from docs/07, `Health`,
      `Cursor`, `SourceItem`
- [x] `backglass/connectors/gmail.py` — incremental via `historyId`, full scan only on
      first run or cursor loss; `gmail.readonly` scope and nothing else; quoted thread
      history stripped **before** hashing; cursor in the `credential` row
- [ ] `tests/fixtures/gmail/` — **vcrpy cassettes not recorded.** Cannot be until the
      OAuth consent flow has run at least once. Substituted a `FakeGmailService` driving
      hand-written fixtures in the documented `users.messages.get` response shape. No test
      hits a live API, which is the rule that mattered; re-record as real cassettes after
      `backglass auth`.
- [x] `backglass sync --dry-run` prints what it would write, writes nothing

## Session 3 — extraction

- [x] `backglass/extract/schemas.py` — Pydantic v2 models are the only definition of shape;
      JSON Schema generated from them, fed to `--json-schema`
- [x] `backglass/extract/prompts.py` — loads `specs/extraction-prompts/*.md`, parses
      frontmatter, stamps the version on every row. No prompt inlined in Python.
- [x] `backglass/extract/client.py` — `ModelClient` protocol; `ClaudeCLIBackend` (flags
      below, mandatory); `DeepInfraBackend` for the product path. Backend chosen by config.
- [x] `backglass/extract/rules.py` — tier-0: bulk headers, `List-Unsubscribe`, `no-reply@`,
      known-noise senders, calendar invites. Records which rule fired in `triage_reason`.
- [x] `backglass/extract/triage.py` — tier-1, model, kill rate measured
- [x] `backglass/extract/dates.py` — relative dates resolve against
      `source_item.occurred_at`, never `now`
- [x] `tests/test_dates.py` — **written first**. Three-week-old fixture; Phoenix→Kolkata
      move in both directions.
- [x] `backglass/extract/entities.py` — alias-table resolution, create on miss
- [x] `backglass/extract/commitments.py` — tier-2 plus post-processing steps 1–5, in code
- [x] `tests/fixtures/commitments/` — all seven cases listed at the bottom of
      `specs/extraction-prompts/extract-commitments.md`
- [x] `backglass/sync.py` — orchestration, `run` row, dry-run, degrade-on-cap, non-zero
      exit on any source failure
- [x] `tests/test_idempotency.py` — sync twice over frozen fixtures, second run writes zero
- [x] `evals/eval_triage.py`, `evals/eval_commitments.py` — real model calls, **not** tests,
      not pass/fail, never gate CI

---

## Mandatory Claude CLI invocation

Measured: $0.0031/call with these flags vs $0.307 without. Non-negotiable in
`ClaudeCLIBackend`.

```
claude -p --model <haiku|sonnet> --safe-mode --tools "" \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --disable-slash-commands --no-session-persistence \
  --output-format json --system-prompt <prompt> --json-schema <schema> \
  --max-budget-usd <per-call ceiling>
```

Read the validated object from the `structured_output` field of the result JSON, not from
`result`. ~5s latency per call, so the sync uses bounded concurrency.

---

## Deviations and spec gaps found while reading

Recorded rather than papered over, per `/goal`. Each needs a doc edit or a ruling.

1. **docs/10 §Model layer says "Anthropic SDK" and "force structured output through tool
   use."** We are using the Claude Code CLI. `--json-schema` preserves the schema-enforcement
   guarantee (it is implemented as a forced tool call), so the deviation is the transport,
   not the guarantee. DeepInfra is OpenAI-compatible and restores the SDK shape on the
   product path. → record in docs/10.
2. **`credential` is `UNIQUE(user_id, source)`, but Phase 1 ingests two Gmail accounts.**
   Two rows with `source='gmail'` are impossible. Minimal fix: `source='gmail:personal'`
   and `source='gmail:asu'`, with the connector name parsed from the prefix. No schema
   change. → needs a ruling before Session 2.
3. **`.env.example` has singular `OWNER_EMAIL`;** the owner has two addresses. →
   `OWNER_EMAILS` (comma-separated).
4. **`specs/schema.sql` has no `schema_version` table** but docs/10 requires one. → added
   in `0001_initial.sql`.
5. **Post-processing step 2 ("apply the type default for `estimated_minutes`") is not
   implementable in Phase 1.** Type defaults are specified in docs/04, which is Phase 4,
   and nothing reads `estimated_minutes` until then. → proposal: leave NULL with
   `estimate_source` NULL in Phase 1, implement in Phase 4.
6. **Post-processing step 5 says "fuzzy match" with no algorithm.** → proposal:
   normalized token-set ratio via stdlib `difflib` at a configured threshold. No new
   dependency, and the threshold is tunable from a fixture.
7. **`run.writes == 0` on an idempotent re-run — but the `run` row is itself a write.** →
   reading: `writes` counts domain writes (`source_item`, `commitment`, `entity`), not
   telemetry rows.
8. **docs/08 Option B was chosen, which makes D1–D6 inert at runtime.** The boundary module
   and the D7 test are still built, because D6 exists precisely for the day the denylist
   turns out to be wrong, and switching to `exclude` must then be a config change rather
   than a build.

---

## Bugs found and fixed during the build

Not spec gaps — defects in work done this session, recorded because each one was found by
something other than the test that should have caught it.

9. **Extraction ordered newest-first, so supersession never fired on a backfill.**
   `pending_extraction.sql` ordered `occurred_at DESC`. Post-processing steps 4 and 5 are
   order dependent: a message resolving an earlier commitment can only supersede a row
   that already exists. On a first run the resolution was extracted before the promise, so
   nothing was superseded and the promise was then deduped away against the resolution —
   leaving one open row citing the wrong source and no tombstone. Now `ASC`, with a
   regression test. Found by the end-to-end demo, not by the unit tests, because each
   fixture passed in isolation.
10. **Dedup ran before supersession, which would have deleted the supersession path.**
    A resolving message is by definition about the same thing as the commitment it
    resolves, so the dedup pass always matched it and dropped it before step 4 could run.
    Dedup is now skipped when `resolves` is true.
11. **Comma-separated env lists could not be read at all.** pydantic-settings JSON-decodes
    complex types from the environment before validators run, so `OWNER_EMAILS=a,b` raised
    a `SettingsError`. Every test constructed `Settings(...)` with real lists and missed
    it; it surfaced the first time the CLI was run. Fixed with `NoDecode`, and
    `tests/test_config.py` now reads from the environment.
12. **The spend cap halted triage as well as extraction.** docs/02 specifies degrading to
    *triage-only*; the concurrency helper broke out of both loops. A month that hit the cap
    early would have stopped classifying mail entirely, and the backlog would have looked
    like an empty inbox rather than a paused one.
13. **Two dead code paths, now wired.** `rules.classify` accepted a `noise_senders`
    argument nothing ever passed — docs/02 lists "sender on a known-noise list" as a
    rule-layer kill, so it is now a `NOISE_SENDERS` config field, subdomain-aware, read by
    `sync._rule_pass`. And `Settings.owns()` was unused: entity resolution now returns
    None when the counterparty is one of the owner's own addresses, so a self-addressed
    reminder no longer creates an `entity` row for the owner and a commitment owed to
    themselves, which would have surfaced in the "awaiting others" view.
14. **`executescript` silently discarded the migration transaction.** It issues an implicit
    COMMIT before running, so the surrounding `BEGIN` was a no-op and the rollback path
    then failed with "no transaction is active", masking the original error.


---

# Phase 2 — Morning brief  ✅ built

docs/09 §Phase 2. All seven hard requirements from docs/05 are enforced in code and named
in `tests/test_brief.py`.

- [x] `brief/model.py` — B1 word limit, B2 provenance (a `Line` cannot be constructed
      without it), B3 empty-section omission, B4 cross-section dedup by docs/05 precedence
- [x] `brief/daily.py` — all eight sections plus the docs/05 failure states. Sections 1, 2,
      3, 6 and 7 read the planner and goal tables, which are empty until Phase 4, so B3
      omits them. Not stubs.
- [x] `brief/render.py` — inline styles, table layout, cream on the outer table, black
      section bars, glyph+label on every chip, dashed keyline for review items, B7 pixel
- [x] `brief/deliver.py` — Resend, never the Gmail API; refuses more than one recipient
- [x] `backglass brief [--send] [--date] [--html]`
- [x] Three fixture briefs rendered: normal day (64 words), Gmail auth expired (33),
      fully booked (74). All under the 400-word ceiling, every line sourced.

**Exit criterion — NOT met, and cannot be by code.** docs/09 requires "seven consecutive
daily briefs ... none containing a claim that turned out to be wrong". That is seven
calendar days against a real inbox.

# Phase 3 — Dashboard  ✅ built

docs/06, docs/11 §3, §4, §8.

- [x] `web/panels.py` — the seven panels, each with its declarative empty state
- [x] `web/actions.py` — all seven write-back actions from docs/06
- [x] `web/app.py` — FastAPI, one page, HTMX fragments returned from every write
- [x] Templates matching `design/preview.html`: CSS lifted verbatim, not re-derived
- [x] htmx 2.0.4 vendored with its hash in `web/static/VENDOR.md` — no CDN in the request
      path for a page rendering the owner's commitments, and it works offline
- [x] Sources panel with kill rate, failure keylines, degraded state
- [x] Keyboard map j/k/x/d/s/r
- [x] `backglass dashboard`, loopback only
- [x] Exit criterion **partially met and tested**: `test_resolving_removes_it_from_tomorrows_brief`
      asserts the docs/09 §Phase 3 criterion directly. The other half (a source auth
      failure visible within one cycle) is asserted in `test_the_sources_panel_names_a_failure`.

## Further bugs found and fixed

15. **A rejected commitment would have been resurrected by re-extraction.** docs/11 §4 says
    reject "tombstones it so re-extraction does not resurrect it", but the dedup pass only
    compared against `status = 'open'`. Now `('open', 'dropped')` — and deliberately not
    `done`, because a promise kept last month and made again this month is a new
    commitment, and suppressing it is how a recurring commitment silently disappears.
16. **B1 could not actually hold.** Section-level truncation cannot get under 400 words
    when one section alone exceeds it. Added a line-level fallback within the last
    remaining section, which states how many it dropped.
17. **The Sources panel hid the kill rate when no credential row existed.** The kill rate,
    the spend state and the last run are properties of the pipeline, not of whether a
    credential happens to exist.

## Deliberately not done

- **vcrpy cassettes** — still blocked on the OAuth consent flow having run once.
- **Inline "Reconnect" OAuth button** (docs/11 §8 step 4) — the consent flow needs a
  browser redirect the CLI already owns. The Sources panel names the exact command
  instead of pretending to run it.
- **Drag-to-resolve on the board** (docs/06 §Panels) — resolve/snooze/drop are buttons and
  keyboard actions. Drag is a third input path for an action that already has two.


---

# Phase 4 — Schedule and goals  ✅ built

docs/09 calls this the largest phase. All of P1–P16, G1–G14, C1–C6 and W1–W3 are
implemented, and **all nine acceptance criteria in docs/04 §5 are tests** in
`tests/test_planner.py`, each named after its bullet. 47 tests.

Built in the order docs/09 §Phase 4 specifies:

- [x] **1. Capacity and estimates** — `plan/capacity.py`, `plan/estimates.py`,
      `plan/timezones.py`. Capacity is computed before any selection and is a hard
      constraint, not a display value. The reserve is clamped to >= 1 minute because "a
      plan that fills every minute is a plan that fails at 10:15". Estimates close the
      Phase 1 deviation that left `estimated_minutes` NULL — the type-default table lives
      in docs/04, which is this phase.
- [x] **2. Day planner** — `plan/planner.py`. Selection, the §1.5 precedence ordering,
      the protected block in the peak window, overflow reported and never truncated
      silently. Work is only ever placed inside a free slot, which makes P5 structural
      rather than checked.
- [x] **3. Rollover and shutdown** — `plan/rollover.py`. The drop-or-do question is asked
      exactly once. A skipped shutdown infers completion from ledger state and produces no
      copy at all.
- [x] **4. Goals, targets, checkpoints** — `goals/targets.py`, `goals/checkpoints.py`.
      Progress is summed from checkpoints on read, so G10 ("deleting a checkpoint
      recomputes progress immediately") is true by construction — there is no cached
      counter that can go stale.
- [x] **5. Staleness and risk, independently** — `goals/health.py`. Two separate
      functions returning two separate types, and deliberately no function that combines
      them into a "health" score.
- [x] **6. Checklist** — `goals/checklist.py`. Cap enforced with an error that explains
      itself; streaks count only *scheduled* days so a weekend cannot break one.
- [x] **7. Monday planning and Friday retro** — `brief/weekly.py`. W1 is structural:
      `daily.build_for` returns the Monday brief instead of building the daily sections,
      so no path can emit both.
- [x] `backglass plan [--date] [--accept]`, `backglass shutdown [--done] [--learned]`

**Exit criterion — MET.** docs/09 §Phase 4 points at docs/04 §5, "all of them, including
the Phoenix-to-Coimbatore timezone case". `test_flying_phoenix_to_coimbatore` asserts the
zone switches on the explicit date range, the brief leads with the change, the working
window moves with the zone, and no block lands at 03:00 local. This is the first phase
whose exit criterion could be closed by code alone, and it is closed.

## Notes on two judgement calls

- **Fixed events come from `source_item`, not a new table.** The calendar connector is
  Phase 5; it will write ordinary source items with the event fields in `raw_json`, per
  docs/03 ("if a field only makes sense for one source, it belongs in raw_json"). So
  capacity needs no schema change when that lands, and today it correctly reports a day
  with no known meetings as having none.
- **The protected block falls back outside the peak window** when the peak is fragmented
  but a >= 90 minute gap exists later. P6 says "when capacity allows" and P7 says where it
  goes; protecting a real block somewhere beats protecting nothing, and the brief still
  reports where it landed.


---

# Phase 5 — Widen intake  ✅ built

docs/09 §Phase 5, and docs/07 §Google Drive / §Notes / §Calendar / §Canvas. 25 tests.
"Each is a connector against an extraction pipeline that already works, which is why this
is late and cheap rather than early and expensive" — and it was: no change to the ledger,
the extractor, the brief or the dashboard.

- [x] `connectors/calendar.py` — syncToken incremental, declined excluded / tentative
      counted as busy, travel flagged for the §1.2 capacity model. Events land as ordinary
      `source_item` rows with the event fields in `raw_json`, which is why `plan/capacity.py`
      needed no schema change to start consuming them.
- [x] `connectors/drive.py` — changes feed, Docs/Sheets/PDF text, binary and media skipped,
      `corpora='user'` plus an ownership filter so shared drives are never crawled.
- [x] `connectors/notes.py` — Obsidian, per docs/07's recommendation. No API, no auth, no
      credential row; the cursor is an mtime watermark. A note's own frontmatter date wins
      over its mtime, so editing an old daily note does not silently rewrite its deadlines.
- [x] `connectors/canvas.py` — RFC 5988 Link pagination to completion, `X-Rate-Limit-Remaining`
      read, 403 backoff, **serialized on purpose** per docs/07. A rejected token reports the
      documented cause (institution-disabled tokens) rather than a generic auth error.
- [x] All five wired into `backglass sync`; `backglass auth <label> --source gmail|calendar|drive`
- [x] `launchd/` — four plists plus a README, per docs/10 §Scheduling. No in-process
      scheduler anywhere in the codebase, which is the point.

**Exit criterion — NOT met, and cannot be by code.** docs/09 requires "every source reports
healthy in the Sources panel for seven consecutive days, and triage kill rate is above 85
percent". Both need real accounts and seven real days.

## Bugs found and fixed

18. **The notes cursor re-read the whole vault on every run.** The watermark was truncated
    to whole seconds, so a file modified at 12:00:00.5 was always "newer" than a cursor of
    12:00:00. Full precision now. Idempotency hid it — `content_hash` meant zero writes
    either way, so the only symptom was reading every file forever.

---

# Where this leaves things

| Phase | Code | Exit criterion |
|---|---|---|
| 1 Ledger + Gmail | ✅ | ⏳ needs OAuth against the real inbox |
| 2 Morning brief | ✅ | ⏳ needs seven consecutive days |
| 3 Dashboard | ✅ | ✅ **met** — asserted in `test_resolving_removes_it_from_tomorrows_brief` |
| 4 Schedule + goals | ✅ | ✅ **met** — all nine of docs/04 §5, incl. Phoenix→Coimbatore |
| 5 Widen intake | ✅ | ⏳ needs seven healthy days across real accounts |

231 tests, mypy strict clean, ruff clean, palette validator exit 0.

The three open criteria are calendar time and real accounts, not missing code. The one
thing that unblocks all of them is the same: `cp .env.example .env`, fill in the Google
OAuth client, and run `backglass auth`.

## Still deliberately not done

- **vcrpy cassettes** — cannot be recorded until OAuth has run once. Every connector is
  tested against hand-written fixtures in the documented response shape instead, and no
  test touches the network.
- **Inline "Reconnect" OAuth button** (docs/11 §8 step 4) — needs a browser redirect the
  CLI owns; the Sources panel names the command instead.
- **Drag-to-resolve on the board** — resolve already has a button and a keystroke.
- **`estimated_minutes` actuals from a timer** — docs/04 §6 rules timers out as a
  different product, so the estimate/actual ratio uses scheduled block duration and says
  so.


---

# Phase 6 — Career layer  (approved 2026-07-30, plan: ~/.claude/plans/wobbly-kindling-crayon.md)

Roadmaps (founder/swe/pm/medical presets + AI interview personalization), searchable
networking profiles, multi-page UI (Dashboard/Schedule/People/Roadmaps), quick-add,
follow-up nudges, entity merge. Step 0 = review-card design fixes from owner screenshot.

**Deviations recorded (rulings, per plan):** multi-page vs docs/06 one-page (dashboard
page unchanged); Phase 6 appended to CLAUDE.md build order; interactive interview model
call vs docs/02 ingest-only — via ModelClient, versioned prompt, transcript as immutable
source_item, spend in run(kind='interview'); LIKE not FTS5; merge repoints commitments
only (checkpoints have no entity ref); SourceRef.url fallback for manual/interview.

- [x] Step 0 — review-card polish (quote link ink, source_link /design/ stub fixed to a
      plain span, k-dash label back to full ink, chip-hang flex layout, §7 spacing snap,
      "reject as" label, confidence as label style)
- [x] S1 — migration 0003 + people read layer (profiles/touch + 4 queries, 12 tests)
- [x] S2 — base.html shell + nav tabs + Schedule day/week pages
- [x] S3 — People pages (search-as-you-type, profile, add/edit forms)
- [x] S4 — entity merge (single transaction, audit snapshot, CLI `people merge`)
- [x] S5 — quick-add commitment (manual source_item provenance, board form + `backglass add`)
- [x] S6 — roadmap presets ×4 + engine wiring (steps→milestone targets, cadences→cadence
      targets; progress/staleness/risk read roadmaps with zero engine changes)
- [x] S7 — AI interview (2 bounded calls, degrade-to-preset on cap/error, transcript as
      immutable source_item, spend in run(kind='interview'), 7 tests, no live calls)
- [x] S8 — follow-up nudges in brief (priority 8, ≤3 lines, curated-only, provenance =
      last interaction)

**Bug 20 (found by the e2e demo, again not by unit tests):** one started roadmap flooded
the brief's Goals section with a "0 this week" line per milestone target — weekly
progress is a cadence concept, and `goal_section` now skips targets without a
`weekly_count`; milestones reach the brief through staleness and risk only.

285 tests, mypy strict clean, ruff clean, palette validator exit 0. All four pages
verified rendering against the demo db (screenshots, both roadmap + people flows
exercised through the UI). Independent verifier pass: CONFIRMED, all nine claims
reproduced in a scratch db; demo db checksum unchanged.

**Verifier observations, not yet acted on (need a ruling):**
1. `source_item_immutable` trigger (0002) fires only BEFORE UPDATE — `DELETE` is
   unguarded at the schema level, though docs/03 says raw items are "kept forever".
   Pre-existing, not introduced by Phase 6. → proposal: 0004 adds a BEFORE DELETE
   trigger with a carve-out for `purge-boundary` (docs/08 D6 legitimately deletes).
2. `backglass add` with identical arguments creates a second pair rather than deduping.
   Deliberate (two identical manual adds are two intents), recorded so it is a decision.

---

# Phase 7 — More sources + zero-API-cost note  (2026-07-30)

**Model cost ruling restated:** `MODEL_BACKEND=claude_cli` is and stays the default —
every model call (triage, extract, interview) shells to `claude -p` on the owner's
subscription with the mandatory isolation flags. No API key, no per-token billing; the
monthly cap is shadow accounting. DeepInfra remains the product-path backend only.

Four new connectors, all docs/07 protocol, config-gated, no new deps:

- [x] `connectors/github.py` — one `search/issues?q=involves:@me` query (union of
      assigned/authored/review-requested, no cross-query dedup), `sort=updated&order=asc`
      so an early rate-limit exit leaves a SAFE watermark; boundary applied to emails
      regexed out of title+body. 14 tests.
- [x] `connectors/slack.py` — explicit `SLACK_CHANNELS` only (never workspace-wide),
      conversations.history, raw ts-string cursor (never reparsed); bot/subtype skipped;
      rate-limit returns the cursor the run STARTED with (advancing would skip unvisited
      channels' tails; re-serving is free under content_hash). 16 tests.
- [x] `connectors/imessage.py` — `mode=ro&immutable=1` open of `chat.db`, ROWID cursor
      (skipped rows still advance it), Apple-epoch ns/seconds by magnitude, boundary on
      handles; health names Full Disk Access. 8 tests.
- [x] `connectors/files.py` — drop folder, full-precision mtime watermark, md frontmatter
      date wins; .txt/.md only (no PDF dep exists — drive's "PDF text" is a UTF-8 decode,
      recorded for a docs/10 ruling); unsupported counted under excluded_by_rule, never
      inflating the D5 boundary tally. 9 tests.
- [x] config + .env.example + `_all_connectors` wiring

**Recorded, needs a ruling:** `github`/`slack` names carry no `:label` suffix, unlike
gmail/calendar/drive. Single-account is fine for a personal tool today, but a second
GitHub account later means a `github:<label>` rename plus a credential-row migration.
Both connector authors flagged it; deferred deliberately.

331 tests green for this phase's scope (one dashboard test excluded — it belongs to the
concurrent sidebar-rework session: the failure banner moved into `_sidebar.html` and
that session owns updating `test_the_sources_panel_names_a_failure`).

## Source on/off switch (same day)

- [x] Migration 0004: `credential.enabled` — a pause, not a removal; cursor and stored
      items survive a pause/resume cycle (tested)
- [x] `credentials.set_enabled` / `disabled_sources`; `_all_connectors` filters —
      config says what CAN run, the credential row says what DOES
- [x] Sources panel: Pause/Resume per row; paused rows hollow-square + muted, never
      vermilion — the owner chose the silence, so it carries no alarm ink
- [x] Brief failure section + sidebar alert ignore paused sources (`enabled = 1` in
      `brief_source_health.sql`, `any_failed` respects the flag)
- [x] CLI: `backglass sources` / `sources disable <name>` / `sources enable <name>`
- [x] `tests/test_sources_toggle.py` — 4 tests. Suite 341 green (sidebar rework's tests
      included — that session fixed its own red test).

## Apple-native sources via the OS automation bridge (same day)

Owner asked for "Siri AI" to do the Apple-native work. Ruling recorded: there is no
public Siri API a pipeline can call; the maintained Apple-native surfaces are the
JXA/AppleScript automation bridge and Shortcuts (where Apple Intelligence's "Use
Model" lives). Data access goes through JXA; extraction STAYS on ClaudeCLIBackend
because Shortcuts' model action cannot enforce a JSON schema and the CLI path is
already free on subscription. This also supersedes docs/12 §8's apple-notes-parser
recommendation for Notes — the owner chose the automation bridge over db parsing.

- [x] `connectors/apple_notes.py` — JXA plaintext dump, modification-date watermark,
      Automation-permission failures surface in health(), runner injected for tests
- [x] `connectors/reminders.py` — JXA; incomplete + completed-since-watermark window
      cursor (scripting bridge exposes no modification date; content_hash makes the
      re-read write-free); completion becomes its OWN immutable source_item
      (`<id>:completed`) because 0002 forbids rewriting the original
- [x] config `APPLE_NOTES`/`APPLE_REMINDERS` opt-ins, registry wiring, .env.example
- [x] `tests/test_apple_sources.py` — 6 tests, injected runner, no osascript in tests

354 tests, mypy strict, ruff clean. Eleven sources total.

## OSS extraction research

- [x] `docs/12-source-extraction-research.md` — landed. Prioritized shortlist:
      1. Slack thread replies (`conversations.replies` fan-out — history omits them; a
         promise made in a thread is invisible today) — also note 2025 rate tiers for
         non-Marketplace apps (1 req/min): backfill budgeted in days, slackdump-style
         Retry-After backoff.
      2. iMessage `attributedBody` typedstream decode (pytypedstream + vendored
         NSString-marker fallback; tapback filter via associated_message_type;
         date_edited handling) — post-Ventura messages with NULL text are dropped today.
      3. GitHub notifications API alongside search (typed `reason`, free 304 polling).
      4. pypdf (BSD, pure Python) for drop-folder + Drive PDFs; needs_ocr flag only.
      5. Vendor talon/email-reply-parser regex sets (both abandonware) for quote
         stripping.
      Verdicts: WhatsApp skip (ToS/ban risk); Apple Notes via apple-notes-parser and
      Reminders via JXA-osascript when those sources land. Docs API tabs + calendar
      singleEvents pitfalls recorded for the existing connectors.

---

# Post-build closure (same day)

- [x] `evals/eval_commitments.py` run against live Sonnet — $0.13, **dates 7/7** (the
      CLAUDE.md rule-4 number). Two eval findings for prompt v2, recorded not patched:
      1. Fixture 04: the model dedupes thread restatements itself (returned 1 of 3).
         Better than the canned fixtures assume; code-level dedup stays as the backstop.
      2. Fixture 05: the model returns zero commitments for a resolving message. "Do not
         extract: things already completed" collides with "set resolves=true" — the model
         follows the first rule, so supersession never fires from live extraction. **The
         v2 prompt should reorder those instructions.** Version bump + fixture pass
         required per the spec's own rules.
- [x] Full end-to-end demo: plan → brief → shutdown (skipped/inferred) → next-day
      rollover leading the plan → Friday retro appended → Monday planning replacing the
      brief. Every flow works from one database.
- [x] Bug 19: **cross-year risk projections dropped the year** — "on pace for 5 Jun"
      meant June 2028 but read as the past against "28 Sep". G13 sentence now carries the
      year exactly when the two dates differ on it. Found by the demo, not the tests;
      regression test added. 232 tests.
- [x] `tasks/plan.md` marked STALE with a banner pointing here.


---

# Phase 6 — Dashboard UI rework: sidebar, tabs, in-app alerts  ✅ built

> Built concurrently with (and adapted to) the Career-layer session above. Its
> multi-page shell landed first, so the plan's `?view=` tab machinery was dropped:
> the four pages ARE the tabs, and this rework moved them into a persistent sidebar
> shared by every page. Steps below re-scoped accordingly; original plan follows.

**As built:**
- `base.html`: top tab nav → left sidebar (brand, page nav with count badges, keys
  1–4). Active page = ink bar with paper text. Sticky, own scroll. Collapses to a
  top strip under 900px.
- `_sidebar.html` + `panels.sidebar()`: alerts (source failed / spend cap / kill
  rate / G14 sustained risk / review count — derived from live state, zero stored
  rows), per-goal staleness chip + risk sentence, roadmap step-progress tracks.
  Injected via one `app.py` middleware on `request.state.sb` — zero route edits.
- Old dashboard failure banner removed; the sidebar alert follows the owner to all
  pages instead.
- Goals panel: G12 staleness chips + G13 risk sentences, goal-level signals gated
  to each goal's first target row (docs/06 §Panels Goals finally fully true).
- Today rows: kind color moved from time-gutter sliver to full-height row keyline
  (owner request), same semantic inks.
- Fixes en route: `Staleness.chip()` pluralization ("1 day quiet"); base `.chip`
  was #000-on-paper → invisible in dark mode for unfilled chips (PROTECTED); new
  `.k-plain` neutral chip.
- Tests: TestSidebar ×4 + roadmap-progress-in-sidebar + updated banner assertion.
  341 passing, mypy strict, ruff, palette validator all clean. Verified live on
  demo db, both themes, screenshots.

# original plan (superseded where it says tabs)  ⏳ planned

**Goal.** The dashboard gets a sidebar shell — per-panel navigation tabs, at-a-glance
goal progress/risk/staleness, and an in-app alerts block — while every docs/06 and
design-system rule keeps holding.

**Assumptions stated up front:**
- "In-app notifications" = a persistent, state-derived **Alerts block**, not toasts.
  docs/11 §3 explicitly bans toast feedback ("no page reload, no toast"), so transient
  popups are off the table. Alerts derive from live state, so they appear and clear with
  the condition — no stored notification rows, idempotency untouched.
- docs/06 implies one page showing everything. Tabs deviate. An **Overview** tab keeps
  the everything-at-once view as the default, recorded as a deviation like §Deviations 1–8.

## Steps

- [ ] **1. Shell layout** — `dashboard.html`: two-column grid. Fixed left sidebar
      (~248px, 2px solid black right rule), main content region `#main`. Under 900px
      the sidebar collapses to a top strip (Tauri min width is 900, so rarely hit).
- [ ] **2. Sidebar nav** — new `templates/_sidebar.html`. Eight entries: Overview +
      the seven panels. Condensed uppercase labels, tabular-nums count badges (open
      commitments, awaiting, review count, checklist done/total, failed sources).
      Active entry = solid black bar with paper text (mirrors section-header idiom).
      Keys `1`–`8` switch tabs; existing j/k/x/d/s/r unchanged inside a view.
- [ ] **3. Views + routing** — `app.py`: `GET /?view=<name>` renders that view;
      sidebar links use `hx-get` + `hx-push-url` swapping `#main`. Overview = current
      2×2 grid. Existing fragment endpoints unchanged — write-backs still re-render
      their panel in place within whichever view is open.
- [ ] **4. Goals surfaced** — wire `goals/health.py` `Staleness` + `Risk` (computed
      today, shown nowhere) into `panels.py`: Goals tab gets per-goal progress track
      (done_this_week / weekly_count), staleness chip ("12 days quiet"), risk sentence
      ("on pace for 14 Oct, target 30 Sep"). Sidebar gets a compressed per-goal line:
      name, mini progress track, worst chip. Staleness and risk stay separate — G11–G14
      forbid merging them into one score.
- [ ] **5. Alerts block** — `panels.alerts()` deriving from existing meta only:
      source failed, spend-cap degraded, kill rate < 85%, `sustained_risk()` goals,
      review-queue count. Rendered in sidebar (`templates/_alerts.html`), vermilion/gold
      keyline rows, each row links to the tab that explains it (provenance rule).
      Replaces the current top banner.
- [ ] **6. CSS** — sidebar/nav/alert styles in `dashboard.css` per design system: no
      shadows, no radius, no gradients, five inks only, spacing on the 4/8/12/16/24/32
      scale, tabular figures. Sidebar counts are plain tabular text, not reels — reel
      digits stay ≤3 per view (header count keeps its reel).
- [ ] **7. Polish pass** — spacing rhythm, chip consistency, declarative empty states
      audit across all views.
- [ ] **8. Tests** — extend web tests: every view renders; Overview contains all seven
      panels; alerts appear and clear with state; Goals tab shows staleness + risk;
      write-back fragment swap still works inside a tab; keyboard map present.

## Files touched

`backglass/web/templates/dashboard.html` (+ new `_sidebar.html`, `_alerts.html`),
`backglass/web/static/dashboard.css`, `backglass/web/panels.py`, `backglass/web/app.py`,
`backglass/db/queries/dashboard_goals.sql`, `tests/test_dashboard*.py`.

## Risks

- j/k card selection must scope to the visible view, or focus lands on hidden cards.
- HTMX write-back fragments target panel ids — only swap targets present in the open view.
- `r` (jump to review) becomes a tab switch, not a scroll, on non-Overview views.
- Sidebar goal lines must not become a fourth chart series or a merged health score.

## Verification

`uv run pytest`, `mypy` strict clean, `ruff` clean, `node scripts/validate-palette.mjs`
exit 0 (no new colors expected), then `backglass dashboard` + Tauri shell run, screenshot
both themes.

---

# Phase 8 — Goals tab + Schedule timeline view  (2026-07-30)

**Request.** Goals+checklist and Schedule each get their own tab with a unique view —
not a reshuffle of the dashboard panels. Inspiration pass done (habit-tracker week
grids; Things 3 / Google Calendar vertical timelines; Swiss/editorial dashboards).

**Design rulings:**
- Nav becomes Dashboard(1) Schedule(2) Goals(3) People(4) Roadmaps(5). Dashboard keeps
  its summary panels; the tabs are the deep views.
- Goals page = goal cards grouped by goal (definition_of_done shown, staleness chip +
  risk sentence at goal level, targets with progress tracks) + checklist as a week
  tick-grid (Mon..Sun of current week, today interactive, streak as a quiet number —
  C4: no flame icons, no guilt copy).
- Schedule day view = true vertical timeline: hour ruler, blocks positioned
  proportionally (1px/min), protected hatch, kind keylines, now-rule when viewing
  today, capacity line on top. Whitespace = free time, made visible.

## Steps

- [x] 1. `web/routes/goals.py` — GET /goals; POST /goals/checklist/{id}/{tick|untick}
      → `_check_week.html`; POST /goals/targets/{id}/weekly/{n} → `_goal_cards.html`.
      View-model: week grid from checklist_item + checklist_tick (streaks via
      `goals.checklist.streak`), goal cards from `dashboard_goals` + `goals.health`.
- [x] 2. Templates `goals.html`, `_goal_cards.html`, `_check_week.html`.
- [x] 3. `schedule.py` timeline builder + `schedule.html` rework.
- [x] 4. `base.html` nav + keyboard map 1–5.
- [x] 5. CSS: `.tl*` timeline, `.wkg` week grid, `.gcard` goal cards.
- [x] 6. Tests in `test_web_pages.py` (empty states, seeded render, tick round-trip,
      timeline positioning, nav everywhere).
- [x] 7. `uv run pytest`, mypy strict, ruff, `node scripts/validate-palette.mjs`.

**Built.** All seven steps done. 354 tests green, ruff clean, mypy clean on web/
(two pre-existing errors in apple_notes.py/reminders.py belong to the concurrent
connectors session), palette validator exit 0. Verified live on the demo db in both
themes (screenshots): goals cards + week grid + timeline + now-line all render.
Two fixes found by the visual pass, not the tests: ticked cells lost their green
fill to a CSS specificity collision, and the today-column highlight used fixed-hex
--n-50, which turns into a light band in dark mode — switched to --rule-hair, and
fixed the same pre-existing bug on the schedule week grid's .wday.now.

## Apple Private Cloud Compute triage screen (same day)

Owner clarified the "Siri AI" ask: use Apple's Private Cloud Compute to SCREEN mail/
messages before anything reaches Claude. Built as `AppleShortcutBackend` +
`TriageRouter` in extract/client.py (`APPLE_TRIAGE=1`):

- The one third-party door to Apple's models (incl. PCC) is Shortcuts' "Use Model"
  action; the backend pipes each triage prompt through `shortcuts run <name> -i -`.
- Triage only — binary KEEP:/DROP: line parsed in code, unparseable → keep (the
  triage.md uncertainty rule). Extraction stays on the schema-enforcing Claude
  backend, always. The router discriminates on the schema ("keep" property), the
  same trick the test doubles use.
- Any Apple-side failure (missing shortcut, timeout) falls back to the primary
  backend — degrades to the old cost model, never to unclassified mail.
- Privacy consequence recorded: with APPLE_TRIAGE=1, raw inbox/message content
  stays inside Apple's privacy boundary; only screen-survivors reach Anthropic.
- One-time setup (Shortcuts.app): new shortcut "Backglass Triage" = Receive Text
  input → Use Model (Private Cloud Compute, prompt = Shortcut Input) → Stop and
  output Model Response.
- tests/test_apple_triage.py — 7 tests, fake runner, no live shortcuts.

361 tests, mypy strict, ruff clean.

---

# Phase 8b — UI polish pass via critique workflow  (2026-07-30)

Ran a 6-agent workflow (5 critique lenses: typography / spacing / system-compliance /
dark+a11y / layout+copy over 12 full-page screenshots + code, then a judge that merged
64 raw findings into 12 ranked fixes). All 12 applied, plus 3 cheap fold-ins:

1. `.wrap` width:100% — grid auto-margin shrink-wrap made 4 pages render at ~430–600px
   with the frame jumping on every navigation. Biggest single fix.
2. Dashboard Goals: /wk buttons gated to cadence targets (were on ~10 milestone rows).
3. Deleted decorative 4px colored banner seams (§8 rule 1 violation; cobalt on text
   surfaces broke §3).
4. Sidebar goal names: 2-line clamp + 120px floor instead of nowrap ("St…" fixed).
5. Theme init moved pre-paint into <head> (dark-mode flash killed). Light default kept —
   recorded decision.
6. Dashboard Goals grouped by goal via .lane headers; goal title no longer prefixes
   every target row. Goal-cards kind echo dropped.
7. Action buttons rest quiet (plain keyline), semantic ink on hover; exception: Drop/
   Reject keep resting vermilion keyline (danger-before-click is a safety cue). Fixes
   the .btn.defer missing-black-keyline rule-2 violation.
8. Current /wk cadence marked (ink fill + aria-pressed) on dashboard + goals page.
9. Banner titles restored toward spec scale (22px vs 14px label size).
10. Dashboard grid reordered: GOALS pairs with SOURCES, CHECKLIST takes last half-row.
11. ▲/◆ glyphs on filled staleness chips + sidebar alerts (rule 3: never color alone) —
    via new m.stale_chip() macro, deduped across 3 templates.
12. Kind legend extracted to m.kind_legend(), now on week view too.
Fold-ins: `.wday.now` outline instead of fill (dark-mode stripe erasure — bug from the
morning session), "1 item did not fit" pluralization, banner baseline alignment.

361 tests green, ruff clean, palette exit 0. Before/after screenshots in scratchpad
shots/ and shots-after/. Judge's dropped-items list (date humanization, tick hit-targets,
People checkbox restyle, brief "item(s)" copy, §3 doc amendment for the cobalt work
stripe) recorded in the workflow journal for a future pass.

## Phase 8b follow-up — deferred fixes implemented  (same day)

The judge's dropped-items list, all done except one:
- [x] Humanized due dates — new `panels.due_label` + `|due` filter: "due Fri" inside a
      week, "due 14 Sep" same year, year only when it differs. Wired through
      m.due_text, _awaiting, person.html (route now passes today). 4 unit tests.
- [x] Tick hit-targets — `button.box::before` inset -6px halo (28px target, geometry
      untouched), checklist + week grid.
- [x] People checkbox restyled into the .box language (appearance:none, square, 2px
      keyline, green+tick when checked) — was rounded UA control, invisible in dark.
- [x] Brief "N item(s)" pluralized in daily.py ×2 + model.py; test_brief updated.
- [x] §3 doc amendment — cobalt's schedule work-stripe recorded as a series-token use.
- [x] Focus rings vermilion → ink (global, inputs, .card) — alarm ink was decorative.
- [x] Polish: .cap .over 2px keyline (rule 2), .chip 10→11px, .sec .inner bottom
      inset, .snav last-child seam dedup.
- [ ] Sidebar sustained-risk alert vs GOALS block dedup — needs owner ruling on what
      ALERTS means (content policy in panels.py, not visual).

363 tests green, ruff clean, mypy web clean, palette exit 0. Spot-checked live:
dashboard due labels + people checkbox render right.

---

# Phase 12 — Personal knowledge base ("Memory")  (2026-07-31)
(approved; plan: ~/.claude/plans/crispy-noodling-storm.md)

Owner ask: a knowledge base backend so Backglass carries personal memory and nothing
re-derives facts from mail every session. Fits the one idea: a fact is a typed claim
with provenance and a lifecycle — the commitment pattern minus a due date. No vector
store; subjects + keys are the index.

- [x] Migration 0008 `fact` (subject/key/value/note/source/source_item_id/status/
      superseded_by) + partial index on active; schema.sql pointer comment
- [x] backglass/facts.py — remember (auto-supersedes same subject+key, local stamps),
      recall, forget (retract, never DELETE), export_markdown (per-line provenance)
- [x] Memory page /memory — subject lanes, add form (datalist of lanes), Forget
      button; nav entry + key 6
- [x] CLI: backglass memory / set / forget / export [--out]
- [x] CLAUDE.md §Owner memory — assistants read `memory export` before searching
      mail/files, write new durable facts back with `memory set`
- [x] Seeded 18 facts from this session (source=assistant, evidence in notes):
      identity, education (incl. Early Start DECLINED + unresolved Aug 5–15
      attendance conflict), housing (Willow 502, move-in Aug 9 8am owner-stated),
      premed cycle, Banner shift, OrgTruth, family, contacts, theme preference
- [x] tests/test_facts.py — 8 tests (supersession chain, retraction survives in
      table, export active-only, page round-trip, nav, 422); migration list → [1..8]
- [x] 508 tests, ruff, mypy strict, palette clean; live: /memory renders seeded
      lanes, `backglass memory export` prints the doc

Not done (recorded): fact extraction from email at ingest (source='extraction'
column ready — needs its own versioned prompt + fixtures; natural Phase 13); facts
never enter the brief (memory is reference, not news).

Concurrent-session note: activities layer (0007) untouched; shared files got
additive edits only, re-read before each.

---

# Phase 11 — Personalize to Dharsan  (2026-07-30)
(approved; plan: ~/.claude/plans/crispy-noodling-storm.md. Owner rulings: apply
June 2029 / matriculate 2030; working window 10:00–22:00.)

- [x] .env: OWNER_NAME=Dharsan (feeds extraction + interview + i_owe resolution),
      WORKING_WINDOW=10:00-22:00, PEAK_WINDOW=10:00-13:00, NOISE_SENDERS = the two
      parent-targeted ASU lists. BRIEF_TO deferred until RESEND_API_KEY exists.
- [x] timezones.local_now_iso(): manual provenance stamps are owner-local with
      explicit offset (P13 kept via absolutes). Used by quick-add source_item
      occurred_at + both totals-log endpoints. Kills the "-1d"/tomorrow bug class
      at the write site; telemetry stays UTC.
- [x] POST /goals/targets/{id}/log — totals loggable from the Goals page (non-
      roadmap goals had no write surface); 404 non-total/inactive, 422 amount<=0.
- [x] Medical roadmap redated to the real cycle (data op, adjust.redate_step):
      MCAT 2029-04-26, AMCAS 2029-06-15, Step 1 2032-06-01, match 2034-03-20;
      goal target recomputed to 2034-03-20.
- [x] Seeded "Barrett move-in: confirm slot and logistics" due 08 Aug (assumption:
      sourced from the Barrett digest; owner should confirm the real date).
- [x] Suite-vs-.env leakage fixed at the root: conftest settings fixture pins
      behavior-shaping knobs (window/peak/tz/noise) to documented defaults — the
      owner's live .env can no longer move planner fixtures. Env parsing stays
      covered by test_config.py per the 2026-07-30 lesson.
- [x] 456 tests, ruff, mypy strict, palette clean. Live-verified: new stamps land
      2026-07-30T19:02-07:00; 6 log forms on /goals; provenance reads today.
      Known remainder: rows written before the fix keep their UTC stamps
      (immutable by design) — the one "-1d" on the awaiting panel ages out.

---

# Phase 10 — Hour-total targets, medical preset v2, roadmap progress view  (2026-07-30)
(approved; plan: ~/.claude/plans/crispy-noodling-storm.md)

Owner ask: roadmap that shows progress toward goals + medical extracurricular list
with hour logging toward the application ("or my app among other things" → generic).
Design: new target kind `total` — lifetime accumulator, total_count to reach,
checkpoint.delta carries each logged amount, note carries org/supervisor (AMCAS
raw material). Progress = SUM(delta) on read (G3/G10). Not doing: per-activity
entities/AMCAS export (later read-only report), hours in capacity model,
auto-extraction of hours (same source field makes it possible later).

- [x] 1. Migration 0006 `target.total_count` + schema.sql kind comment
- [x] 2. Engine: targets.progress lifetime_done + complete; health.risk remaining
      += totals_remaining (observed rate already sums deltas); capacity ignores
      totals (weekly_minutes 0)
- [x] 3. Preset schema `totals` section + medical.md v2 (5 AMCAS categories:
      shadowing 60 / clinical 150 / non-clinical 100 / research 200 / leadership 50)
- [x] 4. Roadmap page: progress header (steps track, next step, staleness chip,
      risk sentence) inside #roadmap-steps fragment; Hours & totals block with
      cumulative bars (green done / gold behind-at-risk), last-3 log entries,
      log + set-target forms → POST /roadmaps/{rid}/totals/{tid}/log|set
- [x] 5. Goal cards + dashboard Goals panel render total targets ("4/60 logged",
      lifetime bar; read-only echo — logging lives on the roadmap page)
- [x] 6. CLI `backglass goals add-total <goal-id> "<title>" <n>` via
      instantiate.add_total (generic to any goal)
- [x] 7. tests/test_goals_totals.py — 14 tests: migration, lifetime sum,
      threshold, behind-pace risk, finished-total no-risk, preset v2 shape,
      instantiate counts + v2 stamp, page render, log/set round-trips, bad-write
      404/422, goal-card echo, staleness clamp
- [x] 8. Gates: 452 tests, ruff, mypy strict, palette exit 0; live demo-db pass
      (medical v2 started as roadmap 2, hours logged, both themes screenshot)

Fixes en route: staleness could read "-1 days quiet" (UTC occurred_at vs local
today) — clamped at 0 with regression test. Phase 9 verifier caveat also closed
this session: heatmap >100% claims (inactive-item ticks in numerator,
unscheduled-day ticks) — active-join + denominator=max(scheduled,ticked), 2 tests.

**Post-verify (same session):** verifier CONFIRMED all 8 claims (XSS-safe notes via
autoescape, 0/0-steps guard, ownership 404s, clamps). It also surfaced a pre-existing
CLI bug: `if __name__ == "__main__": app()` sat mid-file, above the people/roadmap/
sources/goals sub-typer registrations, so `python -m backglass <sub-app>` saw no
sub-commands (console script was unaffected). Guard moved to EOF; both entry points
now expose all sub-apps. 452 tests green after.

---

# Phase 9 — Goals + Schedule: Notion/Excel-template upgrade pass
(approved 2026-07-30; plan: ~/.claude/plans/crispy-noodling-storm.md)

Patterns stolen from Notion/Excel template research, filtered through the design
system: KPI strip (reel budget: exactly 3), GitHub-style consistency heatmap
(§5 sequential magnitude — ink at stepped element-opacity, theme-safe), percent
labels on cadence tracks, week view as proportional 7-column agenda grid with
per-day capacity lines. Deliberately not doing: dense table second view of goals,
drag/drop, streak flames, merged health scores.

- [x] 1. Goals KPI strip — 3 reels (done today / best streak / targets on pace),
      OOB-swapped so tick + cadence fragments keep it live
- [x] 2. Consistency heatmap — 8 weeks × Mon–Sun under the week grid, inside the
      #check-week fragment so ticks re-render it; aria-label per cell
- [x] 3. Percent labels on cadence tracks in _goal_cards.html
- [x] 4. Week view → proportional agenda grid (shared hour window, ~0.5px/min,
      kind keylines, protected hatch, now-rule, links to day view)
- [x] 5. Per-day capacity line in week header cells (planned · free, overflow chip)
- [x] 6. CSS: .kpis / .hm / .trackrow .pct / .wk7 mini-timelines
- [x] 7. Tests in test_web_pages.py (KPI counts, heatmap buckets + labels,
      week positioning, capacity text, pct labels)
- [x] 8. pytest + mypy strict + ruff + palette validator + both-theme screenshots

---

# Phase 8 — Consolidate, upgrade extraction, package, activate
(approved 2026-07-30; plan: ~/.claude/plans/wobbly-kindling-crayon.md)

- [x] S0 commit checkpoint — 4 grouped commits (docs/design, application, desktop,
      tasks), owner authorship verified, no remote so no push
- [x] S1 hygiene:
      0005 `purge_gate` + BEFORE DELETE trigger — raw DELETE aborts, D6 purge opens
      the gate inside its own transaction; prompt v2 (resolutions before the
      completed-things exclusion, explicit carve-out) — live eval $0.14: dates 7/7
      held, fixture 05 now yields the resolving commitment (v1 returned zero);
      04's count X is fixture staleness (model self-dedupes, backstop stays);
      github/slack renamed to `github:personal`/`slack:personal` pre-activation,
      protocol test now covers them
- [x] S2 slack thread replies (fan-out, pre-cursor-thread limitation documented) +
      github notifications feed (two-leg fetch, byte-exact Last-Modified echo)
- [x] S3 imessage attributedBody typedstream (vendored ~58-line NSString scan,
      both length forms), tapback filter 2000–3005, edits stay conflicts
- [x] S4 pypdf (drop folder + drive; needs_ocr flag; encrypted→unreadable count),
      extract/quoting.py vendored from talon+email-reply-parser (with the
      sign-off-needs-a-name-block deviation), docs/10 §Dependencies table
- [x] S5 desktop sidecar — GATE MET: /Applications/Backglass.app serves from the
      frozen tree (tokens.css 200), fresh App Support db w/ all 5 migrations,
      clean sidecar exit, dev mode regression intact. 157 MB.
- [x] S6 docs/13 activation runbook + `backglass doctor` (exit 1 while any check
      fails; the 4 current failures ARE the runbook's remaining owner steps)

437 tests, mypy strict, ruff clean. Verifier pass: 9/10 claims CONFIRMED, one
REFUTED — the doctor's launchd check matched the app's GUI registration (false
green) and missed the real com.cognifer.backglass.* labels (false fail forever).
Fixed with exact-label matching + tests for both directions; lesson recorded.
Verifier caveats accepted and recorded: DROP TABLE is not guarded (SQLite
triggers cannot guard DDL; single-user local db — accepted), and the packaged
app's cwd-relative db self-creation is by design (main.rs pins cwd to App
Support).

What remains of Phase 8 is owner work: runbook steps 2–10 and the seven-day soak.

---

# Med-student layer — PRD written  (2026-07-30)

`docs/14-med-student-prd.md` — full planning/organization gap analysis for the
med-school application track. Eight features: F1 activity registry + AMCAS Work &
Activities export (P0), F2 Anki/Avorio spaced-repetition connectors (P0, Avorio
facts confirmed against /Users/Dharsan/Avorio — local SQLite, FSRS-5, export-view
integration recommended), F3 application cycle tracker (schools/secondaries/
interviews as append-only events, extraction-driven), F4 `metric` target kind for
MCAT FL trajectory, F5 letters-of-rec tracker, F6 prereq/GPA lens, F7 cycle-aware
brief seasons, F8 interview prep packs. Sequenced A–D; five open questions need
owner rulings (§7) before any phase is planned. Owner said "continue with planning
and implementation" → Phase A approved, rulings below taken as working assumptions.

---

# Phase A — Activity registry + spaced-repetition connectors  (2026-07-30, built + verified)

PRD F1 + F2 (`docs/14-med-student-prd.md`). Rulings taken (stated assumptions, owner
can overturn):

| PRD open question | Ruling for Phase A |
|---|---|
| Q1 Avorio surface | Direct read-only SQLite read WITH a schema guard: connector verifies expected tables/columns at open; mismatch → health degraded (rule 5), never a crash. Export view inside Avorio deferred — separate repo. Canonical db = the macOS one; per-device gap recorded in health note if sync ambiguity found. |
| Q2 activity↔total linkage | `activity.category` TEXT keyed to preset total keys (shadowing/clinical/volunteering/research/leadership) + free `other`. No FK to target — survives roadmap re-instantiation. |
| Reviews target binding | Config `REVIEWS_TARGET_ID` (int, optional). Deterministic post-sync step maps review-day source_items → checkpoints on that target. No model call — data already structured. Unset → connectors still ingest, no checkpoints. |
| AMCAS export surface | CLI `backglass amcas-export` (markdown to stdout/--out). Report *page* deferred to a later pass — CLI proves the assembly first. |

## Steps

- [x] 1. Migration 0007: `activity` table + `checkpoint.activity_id` (nullable,
      additive). schema.sql carries the 0007 comment (0006 convention — DDL lives
      in the migration, schema.sql is the 0001 baseline).
- [x] 2. `goals/activities.py`: add/toggle-meaningful/end/list_with_hours/entries_for;
      hours = SUM(delta) over kind='total' checkpoints only, on read; 15-slot and
      3-meaningful counts surfaced never enforced.
- [x] 3. Roadmap page: Activities block + activity picker on the totals log form;
      POST /roadmaps/{rid}/activities, POST .../activities/{aid}/meaningful;
      totals entries now name their activity.
- [x] 4. `backglass amcas-export [--out]`: per activity — org/role/contact/dates/
      hours citing checkpoint ids, note stream as draft material with char count
      vs 700/1325. Assembles evidence, never writes application prose.
- [x] 5. `connectors/anki.py`: immutable read; cursor JSON {revlog: max id,
      due_date}; per-day `reviews:<date>:<max-id>` tallies + once-a-day
      `due:<date>` snapshot (immutability-safe by construction — no item's
      content can ever need rewriting). ANKI_DB_PATH gates.
- [x] 6. `connectors/avorio.py`: same shape; watermark = MAX(reviewed_at) full
      precision (sub-second blind spot documented — UUID PK, no monotonic id);
      required-column schema guard names drift in health(). AVORIO_DB_PATH gates.
- [x] 7. Wiring: `goals/reviews.py` deterministic pass in sync (after extraction,
      dry-run skipped, counted in report.writes); same-day later batches get
      delta=0 markers so a day counts once; brief Goals line "Reviews: N due
      (~M min) · K-day streak" with SourceRef provenance, omitted on quiet days;
      capacity subtracts due × trailing sec/card (measured, 8s fallback),
      new Capacity.review_minutes field.
- [x] 8. Tests: tests/test_med_phase_a.py — 23 tests incl. second-fetch-yields-
      nothing on both connectors (the 2026-07-30 cursor lesson, asserted
      directly), zero-delta same-day batch, schema-drift naming, capacity
      reduction by measured pace, brief line + quiet-day omission, web
      round-trips. test_migrations version lists → [1..7].
- [x] 9. Gates: 484 tests green, mypy strict clean, ruff clean, palette exit 0;
      live demo-db pass (migrated to 0007, activity added + 4h logged through
      the UI, amcas-export renders with checkpoint citation, both themes
      screenshot via Safari). Uncommitted — owner has not asked for a commit.

**Verifier pass:** 9/10 claims CONFIRMED on first pass; one REFUTED + two material
findings, all fixed same session with regression tests (5 new, suite 479→484):
1. Batch external_id `reviews:<day>:<max-id>` collided on rescan (immutability
   conflict, reproduced through the real Ledger) → id now names the exact revlog
   row range `<min>-<max>`; same id ⇒ same immutable rows ⇒ conflict impossible.
   Due item hashed `occurred_at=now()` → pinned to the date's UTC midnight, count
   moved into the id (`due:<date>:<count>`), reader takes newest per source.
2. `immutable=1` vs WAL: both REAL stores are WAL; immutable skips the -wal (stale
   or "no such table" reads, demonstrated live) → `mode=ro` + busy_timeout=2000.
   Lesson recorded. **Same pattern lives in connectors/imessage.py (chat.db is
   also WAL) — pre-existing Phase 7 code, needs its own ruling; not touched here.**
3. Owner's real Avorio data ("8 reviews · 155 min", idle-inflated) drove pace to
   1162 s/card and capacity to ZERO → pace clamped 2–60 s/card, capacity
   reservation capped at 120 min (`REVIEW_CAP_MINUTES`); brief keeps the uncapped
   estimate so the backlog is a visible decision, not a silent surrendered day.
Minor fixes folded in: counted-day probe now filters source='extraction' (a manual
tick no longer swallows the tally); schema.sql carries the 0007 comment only, per
the 0006 convention (verifier flagged the drift; convention kept, recorded here).

---

# Phase A2 — Seamless connector setup  (2026-07-31, built + verified)

**Verifier: CONFIRMED, all 7 claims** (incl. the populated-.env byte-survival probe
and read-only detection proved by a full before/after tree snapshot). Two caveats
fixed same session (+1 test, suite 500): a drifted obsidian.json vault entry could
have 500'd every dashboard render (now degrades to missing, isinstance guard); the
.env.example template was cwd-relative (now: beside the target file, repo-root
fallback). Accepted + recorded, not bugs on the owner's actual LF/newline-terminated
.env: no-change writes normalize CRLF→LF and add a trailing newline; `export`/
indented keys append rather than rewrite (later-wins makes the value still correct);
an inline comment on a rewritten line is dropped (documented behavior).

Owner ask: "all the connectors set up as seamlessly and easily for user as possible."
Design: auto-detection of well-known store locations + one interactive command that
writes .env and binds the reviews target, surfaced everywhere the owner already looks.
Credentials stay in the table (docs/07); paths/opt-ins stay .env — detection bridges
the gap by *finding* the values so the owner never hunts for a path.

- [x] 1. `connectors/detect.py` — probe registry: anki (newest Anki2 profile's
      collection.anki2), avorio (App Support/Avorio/avorio.db), imessage
      (~/Library/Messages/chat.db; invisible without Full Disk Access — hint says
      so), obsidian (vault list from obsidian.json), apple notes/reminders
      (osascript present), plus config-status rows for the token/OAuth sources
      (gmail/calendar/drive/github/slack/canvas) with the exact next command.
      Home dir injectable; no probe in tests touches the real home.
- [x] 2. `envfile.py` — set keys in .env preserving order/comments; creates from
      .env.example when absent; idempotent.
- [x] 3. `backglass setup [--yes] [--env-path]` — table of detections, per-source
      confirm (all-yes flag), writes .env, binds REVIEWS_TARGET_ID by listing
      cadence targets to pick from, prints the exact remaining OAuth/token steps,
      ends pointing at `backglass doctor`.
- [x] 4. Doctor: informational "[ -- ] found but not configured" lines from the
      same detection registry (never failures). Sources panel empty/footer text
      names `backglass setup`.
- [x] 5. Tests (tests/test_setup.py, 15): detection statuses against a fake home tree (newest-profile pick,
      missing, configured), envfile round-trips, setup --yes end-to-end in a tmp
      cwd, panel/doctor surfacing.
- [x] 6. Gates (499 tests, mypy strict, ruff, all green) + docs/13 gains step 0
      pointing at setup. Live dry-drive on the real machine (demo db, scratch
      env): found the real Anki 'User 1' profile + Avorio store, listed the six
      cadence candidates for --reviews-target, wrote nothing real.

---

# UI replan — tournament-judged rebuild of schedule/goals/roadmap pages (2026-07-31)

Three designers (glance / dense / flow philosophies) replanned five pages from
scratch; three judges (daily-use, design-integrity, engineering) scored all four
candidates including the shipping incumbent; synthesis picked winners per page.
Verdict: glance won schedule-day, schedule-week, goals, roadmaps-list; dense won
roadmap-detail (year-grouped steps); incumbent held nowhere but its timeline
geometry, chip grammar, and G-rules survived inside every winner. Banned-ideas
list enforced (no verdict chips, no reel-budget busts, black = literal due-today
only, no write-backs on the read-only schedule).

Built by three worktree-isolated executors, merged sequentially:
- [x] schedule day: NOW/NEXT strip, GAPS ledger, tiny-tier entries, quiet pager,
      P3/P9 as sentences derived from persisted planner state
- [x] schedule week: seven-cell capacity band (free hours 19/26 tabular, gold
      overflow chip in-cell, today outlined), textless grid blocks w/ title attrs
- [x] goals: today's ticks first (#today-ticks), THIS WEEK fold outside swap
      targets w/ OOB pace span, flagged/healthy card split, cadence tick endpoint
- [x] roadmaps list: two-line next-step rows, headline accumulator, CLOSED group,
      structural double-start guard (route redirects; did not previously exist)
- [x] roadmap detail: masthead (≤2 reels + G11-separate staleness/risk), log zone
      first w/ visible provenance, year-grouped steps, NEXT rule+label, 35→2
      boxed-control collapse, read-only cadences linking to Goals

537 tests (508 at checkpoint), mypy strict, ruff, palette validator all green.
Shared due_state_chip macro in _macros.html is the one due-date grammar everywhere.
