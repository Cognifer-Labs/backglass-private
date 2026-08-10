# Files as a knowledge base, on a backend of your choosing

Written 2026-08-10 after checking what already exists rather than proposing from scratch.
Two of the three pieces turned out to be built; this records the third and the one that
is blocked.

---

## Already done — do not rebuild

**File ingestion.** `connectors/files.py` is a drop folder: one file becomes one
`source_item`, reads `.txt`/`.md`/`.pdf` (pypdf, chosen in docs/12 §2 over AGPL
alternatives), runs the docs/08 boundary check before persistence, flags scanned PDFs
`needs_ocr` rather than storing them as empty, and counts unsupported formats out loud
instead of ignoring them. It needs a folder path and nothing else — no API, no auth, no
credential row.

**A knowledge base.** The `fact` table, Phase 12. Subject/key/value with provenance,
supersession instead of UPDATE, `backglass memory export`, and `facts.owner_context()`
already feeds triage. CLAUDE.md is explicit that this is the personal knowledge base and
equally explicit that a vector store, embeddings and RAG are **not** to be built — the
queries are known in advance. A "knowledge base" here means facts with provenance, not a
retrieval index.

**Backends other than Claude Code.** Three existed; `openai_compatible` (commit `b6ed034`)
makes it four and opens every OpenAI-shaped endpoint. `MODEL_BACKEND=claude_cli` is this
machine's `.env` choice, not the code's default — the default is `anthropic`.

| Backend | Needs | Cost |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | real, per token |
| `openai_compatible` | `MODEL_BASE_URL` (+ key if hosted) | real, or zero on localhost |
| `deepinfra` | `MODEL_API_KEY` | real, cheapest at scale |
| `claude_cli` | the Claude Code CLI | **imputed** — a price nobody is charged |

---

## The work: a document becomes facts

Ingestion stops at storing the text. Nothing turns a stored document into anything the
rest of the system can use, so a scholarship letter in the drop folder is a `source_item`
and never a fact, a commitment, or a date on the schedule.

- **Implement**: an extraction prompt for documents, in the shape `extract-commitments.md`
  already uses — a forced tool call against a schema, versioned, with fixtures. It emits
  `fact` rows (subject, key, value) with the source sentence as evidence, and existing
  commitment/engagement extraction keeps its current path.
- **Not**: a chunker, an embedding, or a similarity index. The questions this answers —
  what is my ASU ID, when is the housing payment due, who signed the waiver — are known
  in advance and are `fact` lookups. If a query genuinely needs semantic search, that is
  a separate ruling to argue with CLAUDE.md, not a thing to add quietly.
- **Test**: a fixture document yields the facts it states and no others; a document
  restating a known fact supersedes rather than duplicates; a low-confidence extraction
  goes to the review queue like everything else (rule 2).
- **Runs on**: any backend above. Document extraction is high-volume and low-difficulty,
  which is the profile `openai_compatible` against a local model fits best — and it is the
  one path where nothing leaves the machine, which docs/08 cares about more here than
  anywhere else, since a drop folder holds signed contracts and scanned letters.

## Blocked: a Codex backend

`codex` is on PATH at `~/.superset/bin/codex` and is a shim that reports
`codex not found in PATH`. The real CLI is not installed, so a backend written against it
could not be run or tested — only guessed at, which is how the wrong subprocess protocol
gets committed and discovered months later.

- **When the CLI exists**: the shape is `ClaudeCLIBackend`, which is already almost
  generic — an executable, an argument template, a JSON envelope, and the auth/rate-limit
  signature lists. The honest version of "plug and play" is to lift those into settings
  (`CLI_EXECUTABLE`, `CLI_ARGS`) so a new subscription CLI is a config change, exactly as
  `openai_compatible` made a new HTTP provider one. That refactor is worth doing on its
  own merits and does not need Codex to justify it.
- **Note**: any subscription CLI shares `claude_cli`'s imputed-cost problem, so
  `spend_is_imputed` must be True for it and the cap must keep ignoring it.

## Order

The generic CLI backend first — it is small, testable with a fake executable, and turns
"add Codex" from a build into an `.env` edit. Then document extraction, which is the only
item here that is genuinely new work.
