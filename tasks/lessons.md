# Lessons

Format: `[date] | what went wrong | rule to prevent it`

2026-07-30 | A bare `claude -p` call inherited the full session context (CLAUDE.md, MCP
tool schemas, skill listings) — 29,919 cache-creation tokens and $0.31 for a 17-token
answer. | Every pipeline invocation of the Claude CLI must pass `--safe-mode --tools ""
--strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands
--no-session-persistence`. Measured 100x cost reduction. Never shell out to `claude`
without them.

2026-07-30 | Every commitment fixture passed in isolation, so an order-dependent bug
(supersession never firing on a backfill, because extraction ran newest-first) survived a
green suite and only appeared in an end-to-end run over the whole fixture set. | When
post-processing steps are order dependent, one test must run the whole set through one
ledger in the order the pipeline actually uses. Per-item fixtures cannot see ordering.

2026-07-30 | Constructing `Settings(...)` directly in every test hid the fact that
`OWNER_EMAILS=a,b` could not be parsed from the environment at all — pydantic-settings
JSON-decodes complex types before validators run. The failure appeared the first time the
CLI ran, not in CI. | Test config objects the way production builds them. If production
reads the environment, at least one test must read the environment.

2026-07-30 | Idempotency masked a cursor bug. The Obsidian connector truncated its mtime
watermark to whole seconds, so every note was re-read on every run — but content_hash
meant zero writes, so the sync looked perfectly idempotent while doing all the work
twice. | An idempotency test proves nothing about whether work was *avoided*. When a
connector has a cursor, assert the second run fetches nothing, not just that it writes
nothing.

2026-07-30 | Two sessions built concurrently in this checkout and the worktree escape
hatch was dead: the repo's initial commit is empty, so every source file is untracked
and a worktree would contain nothing. Had to serialize by watching mtimes instead. |
Commit a baseline before any parallel-session work — worktree isolation only isolates
what git tracks. An all-untracked repo cannot be shared safely at all.

2026-07-30 | Wrote a test asserting no guilt-copy in the Monday brief with a substring
check, and "again" matched inside "against" in an unrelated capacity sentence. | Banned-
word assertions need word boundaries. A substring check on short words fails on the
innocent case and teaches you to loosen the test.

2026-07-30 | The doctor's launchd check was wrong in both directions — a substring
match went green on the desktop app's transient GUI registration (zero jobs
installed) and would have stayed red against the real com.cognifer.backglass.*
labels — and no test covered it, so every gate was green around a check that could
never work. Found only by the fresh-context verifier. | A preflight check earns a
test for BOTH its pass and its fail branch, against realistic output strings —
a check nobody has seen fail is a check nobody has seen work.

2026-07-30 | Opened the Anki/Avorio stores with `immutable=1` copied from the iMessage
connector, but both real stores are WAL — immutable makes SQLite skip the -wal file, so
reads were either silently stale or failed with "no such table" while the app was open.
Caught only by the fresh-context verifier probing the owner's real files. | `immutable=1`
is only for files that truly cannot change (snapshots, archives). For a live store,
`mode=ro` + busy_timeout; and check `PRAGMA journal_mode` of the actual production file
before choosing an open mode, not the fixture's.

2026-07-30 | Derived a source_item's external_id from a fetch batch's high-watermark
(`reviews:<day>:<max-id>`), so a rescan spanning old batches re-emitted the same id with
bigger numbers — an immutability conflict the 0002 trigger turns into a failed-looking
sync. Same bug in a second shape: a due-count item hashed `occurred_at=now()`. | Under an
immutable ledger, an external_id must name content that can never be recomputed
differently: key it to the exact immutable row range it summarizes, and make every
hashed field deterministic. "Deterministic given the db" is not enough — it must be
deterministic given the id.

- 2026-08-01 | batch collect wrote a run-level spend accumulator into each batch's
  per-batch spend_cents row (verifier caught it; only surfaced with 2+ batches in one
  collect) | when a loop both accumulates a total and writes per-group rows, keep two
  variables — `group_x` reset inside the loop, `total_x` summed from it — and always
  test the N>1-groups case, not just N=1.

- 2026-08-01 | panel markup change (section → details) broke 3 tests that carved panels
  out of rendered HTML with `.split("</section>")` — and the mechanical fix
  (`</details>`) would have passed while silently truncating at the board's nested
  Quick-add fold, testing a fragment of the wrong shape | two rules: (1) before changing
  shared markup structure, grep tests for structural couplings first —
  `grep -rn '</section>\|</details>\|class="panel"' tests/` — and fix the couplings in
  the same change, not after the red run; (2) tests slice fragments on stable ids via
  one shared helper (`tests/conftest.panel_slice`), never on closing tags or element
  order — a re-composition then breaks zero slices or one helper, never N call sites.

- 2026-08-01 | Visual QA of the dashboard was run against the REAL owner db
  (data/backglass.db) with a live Safari tab; during keyboard-shortcut testing a
  stray `x`/click resolved commitment 21 ("Withdraw from or confirm BioBridge") —
  write-back is real, so a browser test IS a db write risk. Caught in the access
  log (`POST /commitments/21/resolve`) and reverted by exact-match UPDATE.
  Rule: never point a live-browser session at data/backglass.db for testing —
  launch the app against a copy (or backglass-demo.db) via BACKGLASS_DB/temp
  copy first; treat every dashboard surface as mutating; diff open-commitment
  counts before/after any browser QA session.

2026-08-02 | Scout claimed git history was PII-clean; direct re-grep of all revisions found 376 file-hits of the owner's email — and later, a fresh-context verifier caught two leak classes (owner's project names in goal contexts, dangling private-doc citations in shipped --help/comments) that both the scrub gate's banned list and the manual grep missed | Scrub lists are provisional by nature: verify scouted claims that decisions hinge on yourself, and always run an independent adversarial sweep with freshly-derived terms (project names, doc-reference patterns, username-shaped fixtures) before anything ships publicly.

- 2026-08-02 | The public-release scrub rewrote a *comment* inside an already-applied
  migration (`0007_activities.sql`, dropping a `docs/14` citation). Byte-checksummed
  migrations mean a comment is not cosmetic: every `backglass` command against the
  owner's db died on MigrationError, and the day planner silently stopped producing
  plans for two days. Found only by smoke-testing an unrelated feature against a copy
  of the real db. | Migration files are frozen bytes, not source — a scrub, a typo fix,
  a reflow all brick every existing database. `tests/test_migrations.py::FROZEN_CHECKSUMS`
  now fails in CI on any edit; when a shipped migration genuinely must change, add a new
  one. And: any repo-wide text pass (scrub, rename, lint) must exclude
  `backglass/db/migrations/`.

- 2026-08-02 | Wrote a launchd plist whose XML comment explained the `--if-missing` flag
  by name; XML forbids `--` inside a comment, so the file would have been rejected at
  load with only a syslog line to show for it. The existing template test only checked
  that no `{{PLACEHOLDER}}` survived. | Renders-without-placeholders is not validity:
  `tests/test_schedule.py` now `plistlib.loads()` every rendered template. Any generated
  file format gets parsed by its own parser in a test, never eyeballed.

- 2026-08-02 | Five connectors (github, imessage, reminders, anki, avorio) shipped without
  ever being added to docs/07-connectors.md — the file the reading order calls the
  connector contract — and nothing noticed because the only checks were per-connector
  tests. Separately, 200 hand-imported `calendar:asu` items cited into the brief while
  the Sources panel showed nothing, because `dashboard_sources.sql` reads FROM credential
  and an import has no credential row. | Wiring is a contract with more than one end:
  registry, gate, .env.example, detect, docs, tests. `tests/test_connectors.py` now
  asserts the registry and docs ends mechanically, and `unmanaged_sources.sql` names
  evidence that no connector owns. If a surface reads FROM one table to describe "all of
  X", ask what X can exist without a row in that table.

- 2026-08-02 | Wrote two regression tests for a `date(occurred_at)` week-bucket bug,
  saw them pass, and nearly shipped. They passed against the *broken* code too: the
  timestamps crossed a day boundary but the query buckets by WEEK, and both times sat
  comfortably inside the week either way. | A boundary test must sit inside the broken
  window of the *specific* comparison under test, not of the bug class in general — a
  day-boundary time proves nothing about a week-bucketed query. Prove it: revert the
  fix, watch the test go red, restore. "It passes" is not evidence until you have seen
  it fail.

- 2026-08-02 | Wrote the boundary doctor check deriving "is a scoped source live" from
  the `credential` table — in the same session that had just recorded the lesson "if a
  surface reads FROM one table to describe all of X, ask what X can exist without a row
  in that table," and against a ledger that already held 200 credential-less
  `calendar:asu` items. The check was silent on the exact data it existed to catch, and
  a fresh-context verifier found it, not me. | Writing a lesson down is not applying it.
  When a lesson lands, immediately grep the working tree for the same shape — every
  other query that derives a population from one table — rather than trusting that the
  next instance will feel familiar. The instance I missed was two hours old.

- 2026-08-02 | `backglass log --on 2026-09-14` filed the hours under today. `local_now_iso(settings, day)`
  takes a `day` argument, but uses it only to choose the timezone — it always stamps
  *now*. The signature reads like backdating and is not. | A parameter that looks like it
  controls the value but only controls a detail of it is a trap with one job: catching
  the next caller. Added `local_noon_iso` for the backdating case and a test asserting
  the stamp lands on the named day in both of the owner's zones. When a helper takes a
  date and returns a timestamp, test that the timestamp is *on* that date before using it.

- 2026-08-01 | Audit found SQLite's `date()`/`datetime()` silently normalize offset-bearing
  timestamps to UTC before comparing, so `date(occurred_at) = date(:day)` dropped every
  Phoenix calendar event after ~17:00 from its own day and bucketed evening checkpoints
  into the wrong week. Three more sites had the same shape (ORDER BY on the raw column
  inverts across -07:00/+05:30). | When a column deliberately stores mixed offsets, NEVER
  compare or sort it as a date/string. Convert the local day to UTC instants in Python
  (`timezones.utc_bounds`) and compare `datetime(col) >= datetime(:from)`. Test with times
  inside the broken window (evening/early morning), not near it — a midday fixture passes
  against both the broken and the fixed query and proves nothing.

- 2026-08-01 | The spend-cap "stop" lived in a generator's submission loop, so it ran
  before the caller charged anything and never fired; both existing cap tests set the cap
  to 0 and only exercised the early-return guard. | A check inside a generator runs at
  first `next()`, not when it reads. When a limit must interact with results, interleave
  submission with consumption (waves) and write the test at a cap the run reaches
  mid-batch — a zero-limit test only proves the entry guard.

- 2026-08-02 | Reverted a fix to prove a test went red, restored the file, and the test
  kept failing — for several minutes I chased a bug in correct code. `__pycache__` was
  still serving the reverted bytecode; `inspect.getsource` showed the right source while
  the interpreter ran the wrong bytes. | The revert-to-prove-red loop needs a cache purge
  on BOTH edges: `find . -name __pycache__ -prune -exec rm -rf {} +` after the revert and
  again after the restore. Source and behaviour disagreeing is the signature — when a
  file's text says one thing and its behaviour says another, suspect stale bytecode
  before suspecting the logic.

- 2026-08-02 | Wrote a regression test for the WAL-unlink ordering that passed against
  the buggy ordering — the "genuine WAL" it built was empty, because closing a SQLite
  connection checkpoints and deletes it. That is the *second* vacuous test this session,
  in the commit that cites the verifier who found the first one. | When the defect is an
  ORDERING, no end-state assertion can catch it: both orderings leave the same files on
  disk once the function returns. Observe the order directly — patch the later operation
  and assert what is true at that instant. And a fixture that "sets up the broken
  precondition" must be proven to have set it up: assert the precondition is real before
  exercising the code, or the test is testing nothing.

- 2026-08-02 | Three separate HIGH defects across two verification rounds, all the same
  mistake: the guard went in at the CLI call site, and the dashboard's own write path —
  the one the app's error messages point owners at — walked straight past it. Overflow
  bound, duplicate-title check, ordering fix: each "fixed", each still fully reachable
  through the web form. | When a rule protects DATA, put it where the data is written,
  not where a user happens to type. Before writing any validation, find every caller of
  the function that does the INSERT and ask which of them the check will cover — if the
  answer is not "all of them", it belongs one layer down. A guard on one of two doors is
  not a guard, and the test that covers only that door will stay green forever.
