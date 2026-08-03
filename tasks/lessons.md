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
