# Reply drafts: answering a thread, in the owner's voice (2026-08-25)

The owner drafted and sent a reply to Todd Altomare today through a Claude skill, using
the Backglass ledger as the source for the thread, the owner's voice and the owner's
facts. Everything that made that draft good is currently outside the app. This plan puts
it inside.

## What exists, and what it cannot do

`backglass/people/reachout.py` already drafts email. It is good and it stays. Three
templates (`thanks`, `warm`, `ask`), named slots filled from the entity row and the fact
table, provenance beside the body, `mailto:` link, "I sent this" logging a touch. CLI is
`backglass reachout`, web is the form on `/people` rendering `_reachout.html`.

Its docstring names three deliberate properties: **no model call**, **zero ledger
evidence is the primary case**, **provenance beside the draft, never inside**. All three
are correct for what it does, which is warm-keeping outreach to someone the ledger has
nothing on.

It cannot reply to a thread. Not a missing feature — a different problem:

| | `reachout` | replying |
|---|---|---|
| Input | a person and a note | a thread of real messages |
| Content | fixed paragraphs, slots filled | must answer what they actually wrote |
| Evidence | one entity row | every message in the thread |
| Voice | the owner's typed `note` | the owner's own prior sends |
| Model | none, by design | unavoidable |

A reply to arbitrary prose cannot be templated. That is the whole of the argument.

## The boundary, stated the way the 2026-08-10 search ruling was

`reachout.py`'s "no model call" is **module scope, not repo scope** — the pipeline calls
models in `extract/`, `triage`, `logic.py`. So this is additive and lives beside it:

- **`reachout` stays zero-model.** Its three templates are not rewritten to call a model.
  A person met once, plus a note, still renders offline and still tests against fixtures.
- **The reply drafter is a separate path** that calls a model because it has to.
- **Neither writes a row.** Both are reads that end in text the owner sends by hand.
  Sending is still what produces evidence: the reply arrives back through `apple-mail`
  on the ordinary path.
- **Nothing in the brief, the planner or the dashboard depends on a draft.** If this
  whole feature vanished, every existing surface stays correct.

## Stated assumptions, so they are not invisible

1. **Draft only. No send path.** `docs/10-tech-stack.md:202` decides a transactional
   provider (Resend/Postmark) and never the Gmail API, and that provider is for the
   morning brief, not for replying as the owner. Scope is draft → `mailto:` → "I sent
   this". The actual sending stays where it was today: the owner's hand, or the Claude
   skill layer outside the app.
2. **No new tables, no migration.** Drafts are ephemeral reads. launchd runs this
   checkout every 30 minutes, so an uncommitted migration goes live and breaks the
   installed app until the sidecar is rebuilt. Zero writes avoids that entirely and
   makes idempotency (rule 3) trivially true.
3. **`tasks/todo.md` is not touched.** The memory-architecture plan is mid-flight at
   increment 1. This is a separate file with at most one pointer line added there.
4. **Voice sample means the owner's own sent mail**, already in `source_item` under
   `apple-mail` with `author` = the owner. No new store, no embedding.

## Increments

Each lands tested and committed before the next starts.

### 1. `backglass/draft/sweep.py` — the deterministic AI-tell sweep

Pure functions, no model, no I/O. This is the piece that makes model output shippable
and it is the piece that can be unit-tested exhaustively, which is why it lands first.

- `findings(text) -> tuple[Finding, ...]` — each with a category, the matched span, and
  the offset. Categories: banned phrase (`I hope this email finds you well`, `just
  circling back`, `I'd be happy to`, `please don't hesitate`, `I look forward to hearing
  from you`), watermark characters (em/en dash, curly quotes and apostrophes, ellipsis
  char, non-breaking space, zero-width, emoji), leaked placeholders (`[Your Name]`,
  `[Company]`), inflated vocabulary (`crucial`, `pivotal`, `underscores`, `robust`,
  `leverage`, `testament`).
- `clean(text) -> str` — the mechanical half only: character substitutions and phrase
  deletion. Never paraphrases; a rewrite is the model's job, not a regex's.
- Findings are advisory and shown; `clean` is applied.

**Also in this increment, and worth doing regardless of the rest:** run the sweep over
`reachout.TEMPLATES` and fix what it finds. The `ask` template currently opens "I hope
things are going well" — that is on the delete-on-sight list, and it has been going out
in the owner's name. Add a test that asserts every template is sweep-clean, so the next
template cannot regress it.

### 2. `specs/extraction-prompts/draft-reply.md` — the prompt

Frontmatter `id` + `version`, loaded at runtime by the existing `extract/prompts.py`
loader, stamped `<id>@<version>` wherever the draft records its provenance. Same
convention as every other prompt; no new mechanism.

The prompt receives: the thread in order, the owner's stance in the owner's words, two
or three of the owner's own prior sends as the voice sample, and the assembled context
from `context.py`. It returns subject and body only.

Instructions carry what today's session proved matters: answer their question in the
first sentence, match their length, never invent a fact absent from the thread, never
describe an attachment (attachment text is not in `body_text`), one ask per email.

### 3. `backglass/draft/reply.py` — the drafter

```
draft_reply(conn, settings, source_item_id, *, stance, ...) -> ReplyDraft
```

- **`stance` is required**, and for the same reason `reachout` requires `note`: a model
  handed a thread and no instruction writes a form letter with better grammar. The owner
  says what they want the reply to do, in their words.
- **Thread assembly** — `source_item` rows whose `title` matches with `Re:`/`Fwd:`
  prefixes stripped, ordered by `occurred_at`. Both sides are in the table. Note the
  columns are `author` and `title`, not `sender`/`subject`.
- **Voice sample** — the owner's own sends from that thread, or their most recent sends
  elsewhere when the thread has none.
- **Context** — `context.assemble()`, the five-tier assembler that already landed. Not a
  sixth tier.
- **Model call** through `extract/client.py`, so the spend cap (rule 7), the fallback
  chain and `model_call` logging all come free. Cap hit degrades to "no draft, here is
  the thread and the stance" rather than overspending.
- **Sweep applied** to the returned body before it is ever shown.
- **Returns a `ReplyDraft`** shaped like `reachout.Draft`: `to_email`, `subject`, `body`,
  `mailto()`, `as_text()`, and `evidence` — every `source_item` id read, every fact used,
  the prompt stamp, the model and tier, and one line per sweep finding that was fixed.
  Rule 1 holds: every part of the draft names where it came from.
- **Two things the ledger will mislead you about**, both hit today and both worth a
  guard: the owner sometimes sends two or three variants of the same email minutes apart,
  so the draft must not assert what the recipient read; and `body_text` never contains
  attachment text, so a message saying "CV attached" has no CV behind it.

### 4. Surfaces

- **CLI** — `backglass reply <source_item_id> --say "..."`, `--json`, mirroring
  `backglass reachout` exactly, including `--dry-run` semantics if reachout has them.
- **Web** — a "Draft reply" control on `/source/{source_item_id}`, which already exists
  (`web/routes/source.py:39`). HTMX posts the stance, swaps in a `_reply.html` panel
  built on `_reachout.html`'s shape: plain `<pre>` body, `mailto:` link, "I sent this"
  logging a touch against the sender's entity when one resolves, evidence in a
  `<details>`. No new spacing scale — the tile rule in `dashboard.css` already governs
  it (2026-08-25 lesson).

### 5. Tests

- `tests/test_sweep.py` — the sweep exhaustively, no model. Includes the assertion that
  every `reachout` template is sweep-clean.
- `tests/test_reply.py` — a recorded model fixture, never a live API (house rule).
  Thread assembly across `Re:`/`Fwd:` prefixes, stance-required error, cap-hit
  degradation, evidence completeness, and the duplicate-send case.
- Web test uses `conftest.py::panel_slice` against a stable `id="panel-…"` marker, never
  `.split()` on a closing tag.
- Idempotency is trivial and asserted: two drafts, zero writes.

## Done means

`backglass reply <id> --say "yes, and here is why"` prints a sendable reply with a
provenance block naming every source row behind it; the same draft appears on the source
page; `pytest` is green; nothing was written to the database; and `reachout`'s three
templates no longer contain a phrase the sweep flags.

---

## What landed (2026-08-25)

All five increments, `pytest` green at 2716 passed.

| Increment | Files |
|---|---|
| 1. Sweep | `backglass/draft/sweep.py`, `backglass/draft/__init__.py`, `tests/test_sweep.py` (35) |
| 2. Prompt | `specs/extraction-prompts/draft-reply.md` — `draft-reply@1`, 3747-char cacheable prefix |
| 3. Drafter | `backglass/draft/reply.py`, `tests/test_reply.py` (39) |
| 4. Surfaces | `backglass reply` in `__main__.py`; `/source/{id}/reply` + `_reply.html` + panel on `source.html` |
| 5. Tests | `tests/test_reply_page.py` (11), motion arrival rule in `dashboard.css` |

### What the work found that the plan did not predict

1. **All three `reachout` templates carried an em dash**, and `ask` opened with "I hope
   things are going well". Both had been going out under the owner's name since the
   module was written. Fixed, and `test_sweep.py` now asserts every template is clean so
   the next one written cannot regress it.
2. **`reachout` claimed an evidence line for `org` on templates that never render it.**
   Two of the three. A provenance list that names a row producing no words is how a
   provenance list stops being read. Now conditional on the template using the slot.
3. **`occurred_at` holds mixed UTC offsets.** The Todd thread stores `…T19:50:36-05:00`
   next to `…T20:01:04-07:00` — adjacent as text, three hours apart in fact. Ordering a
   thread by the raw string hands the model a different conversation than the one that
   happened. `thread_for` now sorts by parsed instant.
4. **Burst detection needed a real window, not string equality.** The owner's three
   variants went out at 19:50, 20:01 and 20:04, which are three distinct minutes. It is
   now consecutive owner messages inside 30 minutes, which is what a redraft actually
   looks like.
5. **The prompt used an em dash while banning them.** Models mimic their prompt.
   `test_reply.py` asserts the prompt file carries no watermark characters — while
   deliberately allowing it to quote the phrases it forbids.

### Verified end to end

`backglass reply 10488 --say "..."` produced a real draft against the live Shufeldt
thread: six messages assembled across `Re:`/`Fwd:` prefixes, three of the owner's own
sends used as the voice sample, 2432 characters of `context.py` in the call, burst
caution raised, and every `source_item` id named in the provenance block.

### Still open, deliberately

- **No send path.** Unchanged from the plan. The draft ends as text and a `mailto:` link.
- **The desktop sidecar is stale.** This touched `dashboard.css`, `source.html` and added
  `_reply.html`; the Tauri app freezes both at build time, so the Reply panel will not
  appear in the installed app until it is rebuilt.

### Plan-versus-landed deltas, so the record matches the code

- Both modules landed under a new `backglass/draft/` package rather than beside
  `people/reachout.py`. That is the "integrate together" steer: one package holds the
  gate both drafting paths pass through, and `reachout` imports the sweep from it.
- The plan named cap-hit degradation as its own tested behaviour. What landed is the
  spend cap already enforced inside `extract/client.py` plus the route's rule-5 catch,
  which renders any failure as a sentence. There is no separate cap-hit test.
