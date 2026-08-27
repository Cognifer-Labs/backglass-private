# The vault Backglass writes, 2026-08-23

The ask: "add knowledge base and retrieval system in obsidian to make sure backglass
works properly", clarified to *make your own vault based on me and information about me,
along with an evolving state document that holds everything major to check against.*

So this is not adopting one of the four vaults on disk. It is a vault Backglass authors
out of the ledger, opened in Obsidian, and a `STATE.md` that says what is true right now.

## What already exists — do not rebuild

| Piece | Where | State |
|---|---|---|
| Knowledge base | `fact` table, `facts.export_markdown` | 50 facts, 19 with provenance |
| Retrieval | `search.py`, Ollama `nomic-embed-text` | 1551 of 1551 indexed, 0 pending |
| Obsidian ingestion | `connectors/notes.py` | built, dark — `OBSIDIAN_VAULT_PATH` unset |
| Ground truth | `state.collect()` + `state.verdicts()` | the whole of STATE.md, already assembled |

The gap is a renderer and one safety guard. Nothing here needs a new table.

## Design

**`backglass/vault.py`** — `export(conn, settings, *, root, now) -> Report`.

```
<vault>/
  Backglass.md      index, wikilinks to everything below
  STATE.md          the evolving check-against document
  Me.md             the owner: facts by subject, config drift, proposed facts
  Facts/<Subject>.md
  People/<Name>.md
  Classes/<Course>.md
  Commitments.md    388 open, grouped — not one file each
  Questions.md      30 open, what Backglass could not settle
  Decisions.md
  Sources.md        connector health and counts
  Inbox/            the one folder the owner writes in
```

**Loop safety.** Every generated file carries `backglass: generated` in its frontmatter
and `notes.py` returns `None` on it. Without that guard, setting `OBSIDIAN_VAULT_PATH`
to this vault means the next sync ingests Backglass's own output as immutable-forever
`source_item` rows and extracts facts from its own facts. A frontmatter marker rather
than a path skip, because a path skip dies the first time the owner moves a file.

**Idempotency (rule 3).** No timestamps in note bodies; `now` is injected and appears
only in STATE.md. Every iteration sorted. Second export with a frozen `now` writes zero
files. A file whose bytes are unchanged is not rewritten, so it does not re-enter the
sync watermark.

**Provenance (rule 1).** Every claim that has a `source_item_id` links to
`http://<dashboard>/source/<id>`. A claim with no source says so rather than implying one.

**Degradation (rule 5).** Export hangs off the tail of `sync`. A vault that is missing,
read-only or full logs and continues; it never blocks ingestion.

## Order — the sequence is load-bearing

launchd runs the *shared checkout* every 30 minutes. Setting `OBSIDIAN_VAULT_PATH`
before the guard is in that checkout is the permanent-pollution failure above.

1. `backglass/vault.py` + `tests/test_vault.py` — export works standalone, no `.env` edit.
2. `notes.py` guard + both-ways test: generated note yields nothing, `Inbox/` note yields one.
3. CLI `backglass vault export`, sync tail, wrapped so it degrades.
4. Merge to the shared checkout — named files only, never `git add -A`.
5. Only then: `OBSIDIAN_VAULT_PATH=~/Documents/Backglass Vault` in `.env`.
6. Verify: export twice → 0 writes; sync twice → 0 writes; open the vault in Obsidian.

Assumption, stated: the vault is `~/Documents/Backglass Vault`, created by the export.
`obsidian.json` is Obsidian's registry and is not written to — the owner opens the folder
as a vault once, by hand.

## Verified, 2026-08-23

| Claim | How |
|---|---|
| The loop is closed | `NotesConnector.fetch(None)` over the live vault: 0 items, 151 generated skipped, 0 boundary excluded |
| The owner's half still ingests | A note dropped in `Inbox/` yields one item dated from its frontmatter; probe removed after |
| Nothing landed in the ledger | `SELECT source, COUNT(*) FROM source_item WHERE source LIKE 'notes%'` → no rows |
| Export is idempotent | Third consecutive export writes only `STATE.md`, which carries the snapshot time by design |
| Sync is idempotent (rule 3) | Two watched `backglass sync` runs; second reports `writes 0` |
| Nothing broke | 2405 tests pass in both trees; ruff clean on every changed file |
| The frozen app cannot ingest | `/Applications/Backglass.app` bundles only `db/` and `web/`; its only connector import is `detect` (read-only) and `credentials`. The stale-sidecar FAIL is the pre-existing one, not a safety gap here |

Left alone deliberately:

- `People/ygomez@.md` — the entity's canonical name is a truncated address. A filename
  artefact of the ledger's own data, not of `safe_name`; fixing it means fixing the entity.
- `content changed for an immutable source_item: canvas:ics:assignment:…` — five errors on
  every sync, predating this work. The ICS feed rewrites assignment bodies; the ledger
  refuses, correctly. Its own problem.

## Round two: the graph, and the vault as ground truth

The first round wrote 151 notes that barely referred to each other, which is a folder of
text files with a graph view over nothing. What Obsidian is for is the backlink pane, so
the export now writes the edges the ledger already knows:

- a commitment links to the person it is with, when that person earned a note;
- a course code inside a sentence links to that class — `_COURSE_CODE` matches the shape,
  the index decides whether it is real;
- a class names its instructors as links and each of them gets a **Teaches** section, so
  the edge exists in both directions rather than half of it;
- facts and obligations run their text through the same matcher.

Two passes now: read the people and the semester, build the index, then render. A link to
a note that was never written is worse than plain text — the graph fills with phantoms and
nothing distinguishes a real connection from a typo.

The matcher is narrow on purpose, and the tests are mostly about what it refuses. Full
names only, because a person called Will would link every "will" in the corpus. Course
codes only where a class note exists. Text already containing `[[` untouched.

`backglass state` grows a `vault` section — root, notes on disk, how many carry the
generated marker, whether the ingest path is the same folder, when it was last exported,
when the ledger last moved — and one verdict, *the vault is newer than the last sync*.
Not a re-render: `vault.render` asks `state` for `STATE.md`, so a probe that rendered the
vault to diff it would recurse. The drift it catches is the one that happens: the export
rides the sync's tail, and a sync whose tail failed leaves a report of a ledger that has
moved on.

The Memory page links each lane to its vault note over `obsidian://`, and shows nothing
when no vault is configured.

### Verified, round two

| Claim | How |
|---|---|
| Edges are real, both ways | `Taught by [[People/Kayode Odumboni]]` in `Classes/HON 171.md`; a **Teaches** section in his note |
| Course codes resolve | Six `[[Classes/…]]` links inside `Facts/education.md`, all to notes that exist |
| The matcher refuses what it should | Tests: a one-word name is never linked, a course with no note stays plain, `[[…]]` text is untouched |
| The loop is still closed | 153 generated skipped, 0 ingested, after the link changes |
| Still idempotent | Two consecutive exports from the shared checkout: `1 written` (STATE.md), `152 unchanged` |
| `state` grades the vault | `[ ok ] the vault is newer than the last sync — exported 23 Aug 23:02` |
| Nothing broke | 2416 tests pass; ruff clean on every changed file |

### What the vault verdict does not check

`exported_at` is read from `STATE.md`'s own stamp, and `STATE.md` is rewritten on every
export because it carries the snapshot time. So a vault where `Commitments.md` failed to
write — a read-only subdirectory, a full disk — while `STATE.md` kept writing would grade
`[ ok ]` forever. The verdict's name claims more than its derivation supports. The honest
signal for a partial write is the `failed` list on the export's own line, which the sync
prints; this check is about the vault being *older* than the ledger, not about it being
complete. Said here rather than left for someone to discover, because a ground-truth
report that overstates one of its own claims is the failure `state` exists to prevent.

### Not deployed yet, and why

The Memory page's `obsidian://` link is served by the dashboard, and the installed app
runs the frozen PyInstaller sidecar (`desktop/src-tauri/src/main.rs`, the
`not(debug_assertions)` branch), not this checkout. `state` reports
`backglass/web/routes/memory.py` and `backglass/vault.py` among `stale_python`, and
`appupdate.py` refuses to rebuild a dirty tree — which this one is, with 80-odd
uncommitted files from concurrent sessions. So until the tree is committed and the sidecar
rebuilt, the link is visible only via `uv run backglass dashboard`. The vault export
itself is unaffected: it runs from the CLI and the launchd sync, both of which execute the
checkout.

## Round three: the export prunes, and the vault reaches the evidence

**A bug, not a feature.** Until now the export only ever wrote and skipped, so the vault
only ever grew — and what it grew was stale claims. A person earns a note by having a role,
an org, a tag or an open commitment; when the last of those goes away they drop out of the
render and their note sat there saying they owed something. A file no longer backed by a
query has no source left to link to, and rule 1 does not stop applying because the writer
walked away.

`_prune` deletes what the render no longer produces, under two conditions that must both
hold, because deleting the owner's own writing would be far worse than keeping a stale
report:

- the file carries `backglass: generated`, and
- it sits where the export writes — `OWNED_DIRS` (`Facts/`, `People/`, `Classes/`) or the
  vault root.

A note the owner wrote in `Facts/` survives the first condition. A generated note they
dragged into `Archive/` survives the second — moving a file is a deliberate act, and a
prune that reached outside those folders would undo it every half hour. Deletions are named
individually on the CLI, never counted, because a deletion is the one thing here that
destroys something.

**`Documents.md`** is the vault's one contact with retrieval, and it makes contact by
pointing elsewhere. A markdown file cannot run a cosine, and a hand-picked list of
"relevant" documents would be the search-box-over-a-pile the product rejects wearing a
vault's clothes. What the note does instead: name every drop-folder document, link each to
its page, and say what it produced — four syllabi that between them yielded four facts and
eleven open obligations, and, when it happens, a document that "stated nothing the ledger
keeps", which a folder listing cannot tell apart from one that did. The searchable count
and the `backglass search find` command are stated at the top. The per-document listing is
capped at `DOCUMENT_LIMIT`, and says so in the file when the cap bites.

### Verified, round three

| Claim | How |
|---|---|
| Stale notes go | A person stripped of their role is removed by name on the next export |
| The owner's notes stay | An unmarked file in `Facts/` survives; a generated note moved to `Archive/` survives |
| A dry run destroys nothing | `--dry-run` lists the removal and the file is still there |
| Documents say what they produced | Real export: `HON 171 Syllabus` links four facts and four open obligations, each to its lane |
| Retrieval is pointed at, not faked | `Searchable: 1554 of 1554 … 0 pending` and the search command, in the note |
| Nothing broke | 2424 tests pass; ruff clean on every changed file |

## Round four: the knowledge base becomes what the pipeline checks against

The owner's ask: the state document should feed logic, and the app should check against
it before deciding. Two halves, both in the pipeline the CLAUDE.md architecture already
runs — nothing new was invented to carry this.

**`context.py` grows two tiers, three and four of five.** *What has already been decided*
states the owner's standing decisions before every triage and extraction call — the gap
this closes is real: the ledger knew on 12 August that BioBridge was dropped, and no model
call was told, so a later mail about it read as a fresh obligation. *The semester* states
the course codes the ledger already holds, read directly rather than through
`courses.load` (which orders by next meeting and would defeat the block's determinism
contract). Both are stated as facts, never instructions, matching the four tiers around
them.

Identity-merge decisions ("are these two the same person?") are excluded from the settled
tier — they are already applied, and their words are generic enough to collide with
anything. The rule for that exclusion (`settles_an_obligation`) lives in `questions.py`,
which is what writes the marker, and `context.py` imports it rather than re-matching the
wording.

**`questions._settled`** is the second line, for what still gets through the context tier —
a commitment recorded before or after a decision that shares its distinctive words. Two
real bugs surfaced building it, both fixed by running it against the live ledger rather
than trusting the design:

- the first draft only checked commitments *after* the decision; the real failure
  (commitment #29, "Withdraw from or confirm BioBridge", recorded 2 August) is *before* it
  — a decision settles what already exists, and `decisions.record` closes only the one
  commitment explicitly handed to it;
- answering any question records a decision whose title is the question, so the naive
  matcher would ask about its own answers — `settles_an_obligation` breaks that loop by
  excluding every machine-recorded decision, not just identity ones.

Never disposes — `logic.py`'s boundary holds, a shared-words match is not a contradiction.
Asks, and the answer is applied: "the decision stands" drops the commitment, naming the
decision id in the note.

**`vault.py`** renders `context.assemble()` verbatim into `STATE.md`, under *What every
model call is told*. Not a second wording — a print statement the owner can read without
adding one, so a tier quietly going empty is visible instead of silent.

### Verified, round four

| Claim | How |
|---|---|
| The motivating gap closes | Real ledger: `WHAT THE OWNER HAS ALREADY DECIDED — BioBridge: I droppped biobridge…` in the block |
| Identity noise is filtered | Real ledger: 3 identity-merge decisions excluded from both the context tier and the detector |
| The detector finds the real case | 1 settled question on the real ledger — the BioBridge commitment recorded 2 Aug, decided 12 Aug |
| The loop it could have created doesn't | Answering a stale question records a decision; that decision does not get asked about (test caught this) |
| Determinism holds | Semester tier reads `calendar:asu` directly, sorted by subject — not `courses.load`'s next-meeting order |
| STATE.md shows the real block | Exported vault's STATE.md carries the exact context block, verbatim |
| Nothing broke | 2435 tests pass; ruff clean on every changed file (two pre-existing errors elsewhere untouched) |
