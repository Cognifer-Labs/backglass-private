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
