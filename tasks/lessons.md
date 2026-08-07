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

- 2026-08-02 | A duplicate-title guard compared with SQLite's `LOWER()` while the
  resolver that answers "which activity is this" compared with Python's `str.lower()`.
  SQLite folds ASCII only, so "Café Latino" and "CAFÉ LATINO" passed the guard as
  distinct and then matched each other in the resolver — the name became permanently
  unloggable and its hours split across two AMCAS rows. | When a guard and a lookup are
  two halves of one question ("is this the same thing?"), they must share one
  normalization function, and it must be the *stricter* engine's. Never let SQL-side and
  Python-side comparison of the same field coexist; write `_norm()` once and route both
  through it. Corollary: a test that exercises the rule directly will not catch this —
  only one that drives the real door does.

- 2026-08-02 | Wrote a page-wide failure banner containing a `chip k-verm`, which broke a
  design test asserting a healthy page carries no vermilion — first inside the markup,
  then again from the inline JS string once the chip was built dynamically. | Alarm ink
  belongs to the alarm, not to the page that might raise one: build the alarmed element
  at failure time, and put its class names in a served .js file rather than inline, so a
  page with nothing wrong contains the string nowhere. Also: when a test that looks like
  it is "about markup" fails on a script, check the test's intent before weakening it —
  moving the code was right, editing the assertion would have hidden a real rule.

- 2026-08-02 | Fixed a timezone boundary case a verifier flagged, and the fix broke the
  mirror boundary — which the next verifier flagged. The two instants were structurally
  identical (the night before an eastward stay begins vs. the day before one); the two
  verifiers simply had opposite intuitions about which zone the owner was "really" in,
  and the config could not tell them apart. | When two correctness demands are the same
  situation viewed from opposite sides, stop looking for the answer that satisfies both
  — there isn't one. Decide on CONSEQUENCE instead: which error is recoverable? Here,
  too-early is a wait and too-late writes a future-dated row into an accumulator with no
  delete path, so the tie goes early, and the docstring says why. And when a fix targets
  one edge of a boundary, write the test for BOTH edges in the same change — the
  regression shipped precisely because only the arrival day was pinned.

- 2026-08-02 | Used backticks inside a `git commit -m "..."` message; zsh executed them
  as command substitution and silently dropped two phrases from the committed body. |
  Commit messages containing backticks, `$`, or `!` go through a heredoc to a file and
  `git commit -F`, never `-m` with double quotes. Check `git log -1 --format=%B` after
  writing a long message — a swallowed word is invisible until someone reads the history.

- 2026-08-02 | Guarded a config parser with `except TimezoneError` in two places, and a
  shape-valid-but-nonexistent date (`2026-02-30`) sailed past both: the regex validated
  the shape and `date.fromisoformat` validated the value, raising a plain ValueError the
  guards never named. It tracebacked out of three CLI commands and three dashboard pages.
  | A function that validates in two stages must raise ONE exception type, or every
  caller has to know both — and callers only ever learn the one they were bitten by.
  When writing `except SomeError` around a parser, read the parser and list every way it
  can fail; if the list has more than one type, narrow it at the source instead of
  widening the catch. A parametrized test over every malformed input, asserting they all
  leave by the same door, is what keeps it true.

- 2026-08-02 | Wrote `test_a_stale_timezone_typo_cannot_break_logging` with the typo in a
  stay that was NOT in effect, and called only the one function I had just fixed. It
  passed for three rounds while the same typo in a stay that WAS in effect 500'd every
  dashboard page and tracebacked `log` and `plan` — because `active_tz` handed the bad
  name to six other readers. | Test the config in the state where it actually bites. An
  inert value exercises the skip path; a live value exercises everything downstream, and
  those are different tests. Two habits fall out: when a fix concerns configuration,
  write the case where the config is IN EFFECT, and drive at least one real door (CLI
  command, HTTP route) rather than only the function under repair — the door is what
  reaches the callers you did not think of.

- 2026-08-02 | An invariant CLAUDE.md called settled ("user_id on every table") had been
  false for eight tables for months, and specs/schema.sql — step 3 of the project's own
  reading order — described 14 tables against 25 live. Both drifted silently because
  nothing checked them. | A documented invariant with no test is a comment. When
  correcting one, add the check that makes the next drift fail loudly (here: a generated
  schema reference plus a test asserting the column exists on every table), and prefer
  generating a reference file over hand-syncing it — hand-syncing is exactly what failed.
  Corollary found the same day: assertions that hardcode a derived list (the migration
  version numbers, twice) train the next author to edit the assertion instead of reading
  it. Derive from the source of truth.

- 2026-08-02 | The public-repo exporter enumerated files with `git ls-files`, which lists
  only TRACKED files. An uncommitted migration was omitted while the tracked code calling
  its table shipped — the export built, passed the scrub gate, and would have been broken
  on first run for everyone who cloned it. | When a build selects inputs from version
  control, a dirty tree is a partial snapshot, not a minor variance: make the tool refuse
  rather than documenting "commit first" in a runbook. Scope the refusal to what actually
  ships, or it becomes noise and the next person adds a bypass flag. Corollary: the
  release pipeline only caught this because it runs the full test suite *inside the
  exported tree* — keep that step, it is the one check that sees what recipients see.

- 2026-08-02 | Swept the same three foreign files (`__main__.py`, `test_doctor.py`,
  `test_release_manifest.py`) into a commit TWICE in one session — first with
  `git add -A backglass/ tests/`, then again with `git add backglass/ tests/` in the very
  next commit, minutes after writing this lesson down. Four earlier lessons already
  describe the sweep. Writing it a fifth time is not the fix. | The rule is mechanical,
  not attentional: **never pass a directory to `git add` in a tree that has pre-existing
  modifications.** Run `git status --short` first, write down the foreign paths, and pass
  `git add` an explicit file list — or `git commit -o <paths> -F msg`, which cannot stage
  anything else. Then read `git show --stat` before the next action. The recovery
  (`reset --soft HEAD~1` + `git restore --staged <foreign>` + recommit) is cheap and safe
  only while nothing is pushed; the real cost is that the commit message described work
  the diff did not match, twice.

- 2026-08-02 | `detect.py` reported iMessage `configured` on a machine where every sync
  had been failing with "unable to open database file". It had a Full-Disk-Access branch,
  but the branch rested on a stated premise — "macOS hides chat.db from unapproved
  processes as if it did not exist" — that is false: the file stats fine, only the open
  is refused. The unreachable branch made the check look covered. | When a guard's
  correctness depends on a claim about the platform, test the claim, not the guard. Probe
  a resource the way its real consumer does (same open mode, same flags) rather than
  asking a cheaper question like `exists()` and assuming the two agree — and treat a
  comment asserting OS behaviour as an untested assertion until something exercises it.

- 2026-08-02 | Wrote the engagement dedup so a shared guest proved two sightings were the
  same plan. Weekly coffee with the same friend then collapsed into one row and silently
  swallowed every later week — a test caught it, but the rule had read as obviously
  correct. | When a match is built from several signals, ask which signal can never
  distinguish the repeating case. People recur by definition, so they cannot separate a
  standing arrangement; only the date can. A signal that is constant across the instances
  you need to tell apart belongs in the guard, never in the short-circuit.

- 2026-08-02 | Shipped a feature with a full green suite, ruff and mypy clean, four
  revert-to-red proofs and a real-data smoke test — and a fresh-context verifier still
  found six defects, two of them rule violations and one a 500 in an existing dashboard
  path I had regressed. Every test I wrote passed; none of them asked the questions that
  mattered. | Self-verification confirms the path you built; it cannot find the paths you
  did not think about. Two shapes recur and are worth checking by hand before claiming
  done: (1) **a new NOT NULL foreign key breaks every existing writer that deletes the
  parent** — after any migration, grep for who deletes rows in the referenced table, and
  assert the invariant from the live schema (`PRAGMA foreign_key_list`) rather than from a
  hand-written list, so the next table fails a test and not a browser; (2) **a guard added
  to one reader of a new column is a guard on one door** — when a confidence/status filter
  goes into one query, immediately grep every other query over that table and ask why it
  does not need the same filter. Both are the "guard at one call site" lesson wearing a
  schema costume.

- 2026-08-02 | Wrote in a commit message that both halves of a timezone fix "were proven
  by reverting to date() and watching the boundary tests go red". One half was: the third
  reader, `people_plans.sql`'s ORDER BY, had no test at all and a verifier's mutation of it
  stayed green. | A claim in a commit message is a claim, and "I proved it" is the easiest
  one to overstate — say it only about the specific assertions you watched fail. When a
  fix touches N call sites, count them in the diff and check off a red run per site before
  writing the word "every".

- 2026-08-02 | Fixed "two same-day plans collapse into one" by separating them on the
  clock, and thereby turned every reschedule into a duplicate: "dinner Friday at 7" then
  "push it to 7:30" became two rows, two overlapping blocks on the day, and two
  byte-identical brief lines (the brief does not print the hour) that nothing in the
  system can delete. The bug I replaced lost an hour; the fix double-booked the day. |
  Before tightening a match, enumerate what ELSE the tightened signal separates. Here the
  clock distinguishes two genuine plans *within one message* and distinguishes nothing
  worth keeping *across* messages, where it is the field most likely to have been
  corrected — so the same comparison is right in one direction and wrong in the other, and
  the fix was to ask which sighting this is rather than to pick a precision. Corollary:
  when a dedup rule stops matching, something must absorb the difference. If no code path
  can merge, supersede or delete the loser, "not matching" means "duplicate forever".

- 2026-08-02 | Mutation-tested seven fixes to prove the new tests bite; two of the
  mutations were no-ops (`rows += [] or [...]` is still the full list; deleting a chip
  condition left the text the assertion actually matched), so two tests looked proven and
  were not. Only noticed because the failure list was shorter than the fix list. | A
  revert-to-prove-red pass has to verify the revert itself: count the mutations against
  the failures before believing any of them, and prefer deleting the block outright to
  editing a condition — a mutation that still computes the right answer proves nothing,
  and it is easier to write than a real one.

- 2026-08-02 | Two consecutive fresh-context verifier passes both refuted work I had
  already checked myself, and the second pass found that a test I wrote specifically to
  pin an ordering bug passed against that bug — the same false-proof shape the commit
  message was calling out in an earlier commit. | Ordering and tie-break bugs need the
  input permuted, not just present: parametrize insertion order, because a single order
  lets an unrelated tie-break (`id DESC`) produce the right answer by accident. And when
  a verifier refutes, expect the repair itself to need verifying — the second pass found
  three defects in the first pass's fixes, one of which was worse than what it replaced.

- 2026-08-02 | Third verifier pass, third refutation, and all three defects were one
  missing line: a dedup hit `continue`d without recording which row it had claimed, so
  the same-response guard was empty precisely when the first candidate matched instead of
  inserting. Every test across three rounds had covered insert-then-insert or
  match-then-insert; none covered match-then-match, so the whole bookkeeping was only
  ever proven on rows that response had created. | When a loop has a "handled it, move
  on" branch and a "made something new" branch, the accumulator has to be fed by BOTH —
  and the test matrix is the cross product, not the diagonal. Write the cases out: for
  two candidates and two outcomes there are four orderings, and the one nobody writes is
  the one where the early-return path happens first. Corollary from the same pass: when
  several stored rows can legitimately satisfy a match, "matches" is not a selection —
  add an explicit ranking (here, nearest start time), because "first row returned" is an
  arbitrary choice that reads as deterministic and silently prefers the oldest.

- 2026-08-02 | Four verifier rounds, and in three of them I picked a different heuristic
  for "is this restatement the same plan": exact time (duplicated every reschedule and
  double-booked the day), then the day (let a 4pm plan repaint an unrelated 9am one out
  of existence), then nearest-time ranking (which picks a winner but never rejects one,
  so the 4pm still landed on the 9am when it was the only row). Each fix was wrong in the
  opposite direction to the last. | When two readings of the same data are both plausible
  and the consequences differ, no comparison of that data can settle it — stop tuning the
  comparison and get a signal. Here the model was already being asked an almost identical
  question for commitments (`resolves`), so `replaces_earlier` cost one schema field and a
  prompt version. The tell that it was time: the third fix's failure mode was the mirror
  image of the first's. Until the signal exists, pick the default by consequence, not by
  likelihood — a duplicate is visible and dismissible, a wrong merge is silent data loss.

- 2026-08-02 | `newest_citation_before` used SQL `MAX(occurred_at)` to find the most
  recent message about a plan. That column deliberately stores each sender's own offset,
  so MAX is a string comparison: "2026-07-16T01:00+05:30" sorts above
  "2026-07-15T20:00-07:00" while being half a day earlier, and a stale message was
  allowed to repaint a time a later one had corrected. The Python side comparing it was
  scrupulous about instants; the SQL that chose the value was not. | An aggregate is a
  comparison. Every rule about not comparing mixed-offset timestamps as strings applies to
  MAX, MIN and ORDER BY on that column, not just to WHERE — and a careful comparison
  downstream cannot rescue a wrong value chosen upstream. When a column is documented as
  "not comparable as text", grep it for aggregates too.

- 2026-08-02 | Added `replaces_earlier` so the model could say "this message moves an
  existing plan", and matched the move by nearness to its NEW time. A move's new time is
  near where it is going, not near the plan it is leaving, so the matcher aimed at the
  destination: it repainted whichever unrelated plan already sat closest to the new slot
  and left the plan that actually moved untouched. The flag was right and unusable —
  half a fact. | A signal that identifies a CHANGE has two ends, and the useful one is
  usually the end you are moving away from. Before adding a flag, write the lookup it is
  supposed to enable and check the flag actually keys it: "this is a move" does not say
  *what* moved, and the commitment side had already learned this — `resolves` ships with
  `resolves_what` for exactly this reason and I copied only the boolean.

- 2026-08-02 | Two more from committing with `git commit -o`: it takes whole FILES, so a
  file I genuinely had to edit (`__main__.py`, for a connector registry entry) carried a
  prior session's unrelated one-line scrub into my commit. `-o` fixes the "stage a whole
  directory" mistake and not the "this file already had someone else's edit" one, and
  `git add -p` is unavailable in this environment. | When a file you must touch is
  already modified, there is no clean split available — so decide deliberately and say so
  in the commit or the summary, rather than discovering it afterwards. Read
  `git show HEAD -- <file>` for every file you did not create.

- 2026-08-02 | Asked to find sources myself rather than request permissions, and found
  Calendar.app already holding BOTH of the owner's Google calendars plus 77 Contacts,
  reachable through the automation bridge that Notes and Reminders were already using —
  no OAuth client, no consent flow, no Full Disk Access. The Google connector had been
  the documented answer for months and was never usable here. | Before asking someone to
  grant access, check what the machine already has. macOS apps sync accounts locally and
  expose them through a permission the project may already hold; "the API for this
  service" and "the data from this service" are different questions. Corollary that only
  running it revealed: two sources describing the same events collide in ways no test
  imagines — the same class in two local calendars under different UIDs, and the same
  instant written `10:30-07:00` in one source and `17:30Z` in another.

- 2026-08-03 | A hard cap enforced against a number that was never a bill. `MODEL_BACKEND=
  claude_cli` is subscription auth, and the CLI's `total_cost_usd` is the API-equivalent
  price of a call, dominated by its own session cache_creation tokens rather than by the
  payload. `SpendCap` summed it into `monthly_spend_cap_cents` and degraded nine
  consecutive syncs to triage-only at 2006c of an unbilled 2000c, and the dashboard
  asserted "extraction paused" — true of the behaviour, false about the cause. | A guard
  must know what it is guarding. Before enforcing a threshold against a reported number,
  ask what happens to that number when nobody is charged: a cost field on a flat-rate
  backend is a *price*, and a price is worth recording and never worth stopping work
  over. The same applies to `--max-budget-usd`, which was aborting real calls against
  the same imaginary money.

- 2026-08-03 | Shipped a page whose job was to let the owner choose which conversations
  to read, and it stayed empty forever. Sightings were gathered inside the fetch loop,
  which only sees rows above the cursor; the cursor was already at the end of a 43,000
  message store, so no chat was ever discovered, so none could be chosen, so the empty
  allowlist that made the page necessary was also what kept it blank. | When a feature's
  input comes from the thing the feature disables, write the cycle down and check it
  breaks somewhere. Discovery ("what exists to decide about") and consumption ("what have
  I already read") are different questions and must not share a watermark. Second half of
  the same bug: saying yes has to reach backwards — a decision made today that only
  applies to tomorrow's messages leaves the plan already made in that group outside the
  ledger, which is the entire reason to monitor it.

- 2026-08-03 | Mail had been missing since the beginning because the Google connector
  needed an OAuth client, and `~/Library/Mail` had 35,376 messages and 35,441 .emlx
  bodies sitting on disk the whole time — the same discovery as Calendar.app in the
  2026-08-02 lesson, one source later. | I recorded that lesson and did not generalize
  it. "Check what the machine already has" is not a fact about calendars; it is the first
  question to ask of every remote source, and the ones still unconnected (Drive, Canvas,
  Instagram) each deserve it asked again rather than a token request sent to the owner.

- 2026-08-03 | `_minutes_apart` subtracted two ISO datetimes wrapped in `except
  ValueError`, and the failure that arrived was a `TypeError` — offset-naive minus
  offset-aware. Calendar.app stores `2026-08-20T10:30:00-07:00`, a message about the same
  event stores `17:30`, and the tolerant guard below the subtraction never saw it. It
  killed a 4,400-message backfill outright. | Catch the exception the operation actually
  raises, not the one that came to mind. Mixed-awareness arithmetic is a TypeError and
  never a ValueError, so a `try/except ValueError` around datetime maths is a guard that
  cannot fire on the most likely fault. And the fourth entry in this file about mixed
  offsets: when a column is documented as carrying an offset *sometimes*, every operation
  on it — compare, subtract, MAX, sort — needs the sometimes case written down.

- 2026-08-03 | Rule 5 was implemented for sources and for model calls and not for
  `apply()`, which re-raised. One item whose application threw ended the run and took
  every item queued behind it, so a single malformed timestamp cost thousands of
  already-triaged items their extraction. | "Degrades, never blocks" has a unit, and the
  unit is whatever the loop is iterating. A rule enforced at the source level says
  nothing about the item level; when adding a loop that processes many independent
  things, ask what happens to items 2..N when item 1 raises, and make the answer explicit
  rather than inherited from whichever `except` happens to be in scope.

- 2026-08-05 | A suite of 1,441 tests was green while `/schedule?date=2026-02-30` was a
  500, a snooze of 10^15 days silently NULLed an open commitment's `due_at`, and
  quick-add wrote the literal text "tomorrow" into the column the board sorts by. None
  of it was subtle; none of it was reachable by any test, because every test drives a
  value someone chose to write down. | Green means the paths you thought of work. Once
  per surface, drive it adversarially instead: every route with a malformed, absurd and
  oversized version of each parameter, then every write against a row that exists, with
  the value at 0, at -1, and at 10^15. Two sweeps of an afternoon found six defects that
  four months of tests had not, and the sweeps became `tests/test_edges.py` so the next
  such value is caught by CI. Corollary worth stating on its own: a number that reaches
  SQLite needs a *ceiling*, not just a floor — `date(x, '+N days')` returns NULL on
  overflow instead of raising, and a NULL means something legitimate in most columns.

- 2026-08-05 | Ten mutations against a new lock module, and four came back green. Three
  were real gaps; the fourth was the defect itself — weakening `LOCK_EX` to `LOCK_SH`
  (which is *exactly* "two syncs can run at once") passed every test in the file,
  because each of them stood in for the other process with a hand-rolled `LOCK_EX`
  probe, and an exclusive probe conflicts with a shared lock just as readily as with an
  exclusive one. | When a test uses a stand-in for the other party, the stand-in must ask
  the question the same way the real party would. A probe that is *stricter* than the
  real caller cannot see the lock being loosened. For anything cross-process, pay the
  50ms and spawn the process running the real code path — and when a mutation pass comes
  back green, do not move on: a surviving mutation is either a missing test or a mutation
  that was not a mutation, and both are worth the five minutes to tell apart.

- 2026-08-05 | Started an audit in the shared checkout, and forty minutes in noticed five
  files modified that I had never touched — another session was running a security pass
  in the same tree. Before that, a test failed for reasons I could not reproduce and I
  spent several minutes chasing my own changes. | The concurrency rule in CLAUDE.md is
  four lessons old and I still only checked *after* something looked wrong. Check first:
  `git status` at session start, then again the moment any result is inexplicable — a
  file changing under you is indistinguishable from your own bug until you look. And the
  tell is cheap to read: `ls -lT` on the surprising file against the time you last
  touched it. Moving to a worktree mid-session cost one `git worktree add` and fifteen
  minutes; the commit race it removed has cost more than that four times.

- 2026-08-05 | Concurrent session reverted `actions.py`/`app.py` to HEAD mid-work,
  destroying three landed fixes (re-applied from a scratchpad snapshot). Same checkout,
  two writers — the exact race the global worktree rule exists for, now with a file-level
  revert instead of a commit race. | Before editing a repo, check for a second active
  session (`git status` churn you didn't cause, processes, mtimes). If one exists, move
  to a worktree or snapshot every change outside the repo (`git diff HEAD > patch`)
  after each green suite. An uncommitted fix in a shared tree is one `checkout --` from
  gone.

- 2026-08-05 | `apple-contacts` had never completed a sync: the JXA script looped
  per-person (`p.name()`, `p.phones()…` = 6+ Apple Events × N cards) and blew the 120s
  osascript timeout; bulk-array fetch (`app.people.name()`, `app.people.phones.value()`)
  returns the same data in ~6 events — 0.6s for the whole book. Also: WebKit never
  focuses a `tabindex` div on mouse click, so a CSS `:focus-within` reveal is
  keyboard-only until a click handler selects the card. | In JXA, never loop property
  reads — fetch whole-collection arrays. In Safari/WKWebView, never gate UI on
  `:focus-within` reaching a div from a click.

- 2026-08-06 | Built a column chart whose markup and tests were all green, and the first
  render showed two defects no assertion could have caught: the current month's "still
  filling" ground fill drew a full-height pale block that read as a second, lighter bar,
  and the pace line — the reference the whole chart is judged against — was occluded by
  every column, so it appeared only in the two months that happened to be empty. | A
  chart's correctness is partly optical and has to be looked at. Two rules fall out: a
  decoration inside the plot area is read as a mark, so state that is not data ("this
  month is incomplete") belongs on the axis, not behind a column; and a reference line
  must be drawn above the marks it references, or it is a line drawn only where there is
  no data. Corollary from the same page: a table idiom borrowed wholesale carries
  assumptions about its own shape — `.wkg` zeroes `padding-left` on every name cell
  because its name column is first, and in a five-column register that ran two headers
  together with no gutter at all.

- 2026-08-06 | Added a column to the activity table, verified it with `curl` (present),
  then screenshotted the page in Safari and saw the OLD table with no such column — and
  spent several minutes doubting the server. mcp-safari had returned a stale capture; a
  cache-busting `?v=2` on the URL produced the real page immediately. | A screenshot is
  a cache, not an observation. When a rendered check disagrees with the served HTML,
  believe `curl` and re-request with a changed URL before touching the code. Every visual
  QA navigation in this repo should carry a unique query param for the same reason —
  the failure mode is silent agreement with whatever you saw last.

- 2026-08-05 | Proved a mutation red, then restored with `git checkout -- backglass/sync.py`
  — which restores HEAD, and my entire uncommitted implementation lived on top of HEAD, so
  the restore deleted the feature it was meant to un-mutate. Re-typed it from the session
  transcript. | `checkout --`/`restore` mean "back to HEAD", not "undo my last edit". For a
  mutate-and-prove-red pass on uncommitted work, save the exact pre-mutation bytes first
  (`cp file file.bak` in the scratchpad, or `git diff > patch`) and restore from that copy
  — never from git — and purge __pycache__ on both edges as before.

- 2026-08-06 | Rebuilt and replaced /Applications/Backglass.app; the app-spawned sidecar
  then hung forever before binding — blocked in a raw `open()` with zero fds, while the
  identical binary served fine from any terminal. The terminal has Full Disk Access; the
  app's TCC grant for ~/Downloads (where the repo, .env and db live) is keyed to its
  code signature, and the fresh ad-hoc signature invalidated it, so macOS parked the
  open on a consent prompt nobody had answered. | A rebuilt bundle is a new principal:
  after replacing the installed app, expect a one-time "access files in Downloads"
  prompt and treat a sidecar stuck pre-bind with ~0 CPU as a permissions hang, not a
  code bug — `sample <pid>` bottoming out in `_io_FileIO___init__ → open` is the
  signature. Diagnose by running the same binary from the same cwd in a terminal:
  if it serves there, the code is fine and the sandbox is the variable.

- 2026-08-06 (addendum) | The TCC prompt recurs on EVERY rebuild: ad-hoc codesign (-s -)
  mints a fresh identity each time, and macOS keys the Downloads grant to it. | The
  durable fix is a stable self-signed signing identity (Keychain Access → Certificate
  Assistant → code-signing cert, e.g. "Backglass Dev"), then `codesign -s "Backglass
  Dev"` in build-sidecar.sh — the grant survives rebuilds. Until that exists, every
  reinstall costs the owner one Allow click, and a sidecar with ~0 CPU stuck pre-bind
  is that click waiting to happen.

- 2026-08-06 | A stray top-level `}` had been silently deleting the entire base `.panel`
  rule from dashboard.css since commit 8ea8ba9 — no panel seams, no closing padding, and
  the documented `min-width:0` overflow fix inert on every page. It hid because the file
  also never closed its last `@media`, so the two errors cancelled and the brace count
  balanced at 514/514. I read the file top to bottom during the rounding pass, saw the
  lone `}` on its own line, and read past it. | A balanced brace COUNT is not a balanced
  file. Compensating errors are the normal case, not the exotic one, because an editor
  that drops a `}` in one place often drops an opener elsewhere. The check that catches it
  is depth, not count: walk the file and assert the running depth never goes negative and
  ends at zero. Corollary, and the more expensive half: when a whole-file read surfaces
  something structurally odd that is not what you came for, spend the thirty seconds then
  — parse it, or grep for its effect. "Not my change" is a reason to flag it, never a
  reason to not look. It took a fan-out of verification agents to find what was already on
  my screen.

- 2026-08-06 | Wrote the concentric-radius rule as "inner = outer − padding" and shipped
  the reel as the canonical worked example at 4px over 2px windows. Wrong: `border-radius`
  is measured on the BORDER box, so the arc a flush child actually meets is the declared
  radius minus the border width. The 4px plate had a 2px inner arc — identical to the
  window it was supposed to sit a step outside of — so the two curves ran flat, which is
  the precise failure the rule exists to prevent. | When writing a geometry rule, state
  which box model edge it is measured from, and verify the example arithmetic against that
  edge rather than against the mental picture. A worked example in a spec is the thing
  everyone copies; getting it wrong propagates further than getting a single rule wrong.
  Here the inset is padding + border, and any rule phrased as "minus the inset" has to say
  so or it will be read as padding alone.

- 2026-08-07 | Changed the radius scale and tile spacing, reloaded the same localhost port,
  saw a pixel-identical page, and nearly concluded the edit was a no-op. Safari had cached
  the stylesheet; `curl` against the same URL showed the new values the whole time. Several
  earlier "verified by screenshot" passes in this session ran against the same origin after
  a CSS edit, so some of them may have been reading stale paint. | A screenshot after a CSS
  edit proves nothing until the bytes are proven fresh. Either `curl` the stylesheet and
  match it against disk before believing the render, or serve on a NEW PORT — the HTTP cache
  is keyed by origin, so a different port is a guaranteed cold cache. Reload is not enough
  and a hard reload is not reachable through the automation.

- 2026-08-07 | Added `.gcard .mlist li` to the `:is(...)` list that styles every tile, and
  silently broke the rule the whole design rests on. `:is()` takes the specificity of its
  MOST SPECIFIC argument, so one descendant selector lifted the entire tile rule from (0,1,0)
  to (0,2,1) — above `.src.cold` and `.rmrow.overdue` at (0,2,0). A going-cold person stopped
  rendering vermilion and became an ordinary tile, in both themes, with 1616 tests green. |
  `:is()` is not specificity-neutral, and its cost is invisible at the call site: the
  selector that breaks the cascade is not the one that changed behaviour. In a grouped rule
  that other rules are meant to override, keep every argument to a single class and assert
  it — a documented precedence order with nothing enforcing it is a comment, not a rule.
  Found by looking at dark mode, not by any test, which is why the test exists now.

- 2026-08-07 | `test_brief.py` asserted `border-radius:{render.RADIUS_CHIP}` — the constant
  checking itself. The radius scale moved to 6px, `RADIUS_CHIP` stayed at 4px, and the brief
  shipped chips a different size from the dashboard's with the test green throughout. The
  same shape had already appeared twice that week: `tokens.json` and design-system.md both
  quoted contrast ratios computed against a paper colour neither of them still declared, and
  they agreed with each other perfectly. | A checker keyed to a mirror cannot detect drift in
  that mirror, and mutual agreement between copies is not evidence — they can be wrong
  together, which is the normal way this fails. A test must read the value from its declared
  authority (or recompute it from first principles) or it is testing that assignment works.
  When a value must be copied because the medium cannot resolve the original — email has no
  custom properties — the copy needs a machine-checked link back, not a comment naming its
  source. `scripts/truth.py` is that link now.

- 2026-08-07 | Used `git checkout -- backglass/web/static/dashboard.css` to undo a two-line
  scratch edit, and destroyed an uncommitted fix elsewhere in the same file. The lesson
  saying exactly this — "`checkout --` means back to HEAD, not undo my last edit" — has been
  in this file since 2026-08-05, written after the identical mistake. Reading it was not
  enough; I reached for the fastest revert under time pressure. | The lesson was right and
  restating it changes nothing, so the rule becomes mechanical instead: never revert a file
  with `git checkout`/`restore` while it carries uncommitted work. `cp file file.bak` before
  the scratch edit and `cp` back, every time — the backup is two seconds and the file-level
  revert is unbounded loss. Better still, do scratch experiments in a copy under the
  scratchpad and never touch the real file. The tell that I was about to do it again: I
  typed the checkout from muscle memory rather than deciding to.

- 2026-08-07 | Merged a 13-commit branch that had rewritten the same design files, and
  resolved six CSS conflicts by taking "theirs" because each one looked like a pure
  token rename. One of them was not: it also carried this branch's panel grounds and
  the detached-grid ruling, which the other branch had never seen. Nothing failed —
  `git` was satisfied, the sheet parsed, and the loss showed up only because a
  HEAD-only test (`TestPanelGrounds`) went red four failures deep in an unrelated
  run. | In a merge where both sides edited the same file, "take theirs" is a decision
  about content, not a formality: before resolving, diff each side against the merge
  base and ask what only ONE side has. A conflict hunk whose two halves differ in
  length by more than a few lines is carrying unique work, not a rename. And when the
  two sides hold contradictory rulings from the same owner (seamless grid vs detached
  panels, both dated this week), that is not a resolution to pick — it is a question,
  and asking cost one message where guessing would have silently deleted a day's work.
