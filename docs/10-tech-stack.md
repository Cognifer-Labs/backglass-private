# Tech Stack

Every choice here optimizes for the same thing: **one person should be able to read the
whole system in an afternoon a year from now.** That constraint rules out most of what a
team-scale product would reach for, and it is the right constraint because there is one
user and one maintainer.

Where a choice is contested, the alternative and the reason for rejecting it are recorded.

---

## Language and runtime

**Python 3.12+.** 3.13 is fine; do not go below 3.12 (the typing and `tomllib` ergonomics
matter more than they sound).

Python wins here on library coverage rather than language merits: the Google API clients,
the model clients, and email parsing are all first-class, and the extraction pipeline is
mostly IO and JSON shuffling where language speed is irrelevant.

**uv** for dependency and environment management. Single tool, lockfile, no separate
venv ritual, and it installs fast enough that a cold CI run is not annoying.

```
uv sync
uv run backglass sync --dry-run
```

Rejected: Poetry (slower, heavier), bare pip + venv (no lockfile), Node for the pipeline
(worse email and Google client story).

---

## Storage

**SQLite**, accessed through the stdlib `sqlite3` with `row_factory` set to return dicts.
Raw SQL in `.sql` files, loaded by a thin helper. No ORM.

The schema is 13 tables and every query in the product is a `SELECT` with a `WHERE`
clause. An ORM would add a layer of indirection over queries you can read aloud.

**Migrations** are numbered `.sql` files plus a `schema_version` table. Apply in order,
record the version, refuse to start if the file set and the recorded version disagree.

```
migrations/
  0001_initial.sql
  0002_add_estimate_source.sql
```

Rejected: SQLAlchemy (defensible, but the mapping layer buys nothing at this size),
Alembic (autogeneration against a hand-written schema causes more diffs than it prevents),
Postgres (correct on the day there is a second user, wrong before that).

Enable `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON` on every connection. WAL
matters because the dashboard reads while the sync writes.

---

## Model layer

Two tiers as specified in `docs/02-architecture.md`:

| tier | model class | why |
|---|---|---|
| triage | smallest available | binary keep/drop over high volume; cost dominates |
| extraction | mid-tier reasoning model | precision matters; volume is 5–10% of intake |

**Force structured output through tool use rather than prompting for JSON.** The model
calls a single tool whose input schema is the extraction schema, so malformed output is
rejected at the API layer and retried, instead of arriving as prose you have to salvage.

**Pydantic v2** models mirror those schemas and are the only definition of shape in the
codebase. Generate the tool JSON Schema from the Pydantic model so the two cannot drift.

Prompts live in `specs/extraction-prompts/*.md` with a version in the frontmatter, are
loaded at runtime, and the version is stamped on every row the run produces. Never inline
a prompt in Python.

### Two backends, one interface

This section originally said "Anthropic SDK" and nothing else. It now names two backends,
selected by `MODEL_BACKEND`, behind the one-method `ModelClient` protocol in
`extract/client.py`. Nothing above that module knows which is running.

| backend | when | marginal cost |
|---|---|---|
| `claude_cli` | now, personal use | none — runs on the owner's Claude subscription |
| `deepinfra` | when this becomes a product | per token, open-source models |

**`claude_cli` shells out to the Claude Code CLI.** The reason is billing, not
engineering: the owner has a subscription and no API key, and a personal tool that costs
nothing per run is a tool that survives the month its enthusiasm wears off.

The structured-output requirement above survives the substitution intact. `--json-schema`
is implemented as a forced tool call, and the CLI returns the validated object in a
separate `structured_output` field of the result envelope. Read that field, never the
`result` text. What changes is the transport, not the enforcement.

What is genuinely given up, stated rather than buried:

- The SDK's retry and backoff. `ClaudeCLIBackend` retries once with the schema restated,
  per `extract-commitments.md` §Failure handling, and parks the item after that.
- Latency. A subprocess per call is roughly five seconds, so `sync.py` runs the model
  passes under a bounded thread pool instead of serially.
- Commercial terms. A consumer subscription is the weakest tier for third-party data, and
  `docs/08` Option B would require the opposite. The two are only compatible because the
  ingested mailboxes carry no client correspondence. **If that stops being true, this
  backend has to go before the mail arrives, not after.**

**`deepinfra` is the product path** and restores the shape this section describes
exactly: OpenAI-compatible chat completions with a forced function call, so schema
enforcement is native and back at the API layer. The deviation above is temporary.

### The CLI invocation is not negotiable

Measured 2026-07-30: **$0.0031 per triage call with these flags, $0.307 without.** A bare
`claude -p` inherits the whole interactive agent context — `CLAUDE.md`, every MCP tool
schema, the skill catalogue — which was 29,919 cache-creation tokens for a seventeen-token
answer. A hundredfold, on the call that runs against every message in the inbox.

```
claude -p --model <alias> --safe-mode --tools "" \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --disable-slash-commands --no-session-persistence \
  --output-format json --system-prompt <prompt> --json-schema <schema> \
  --max-budget-usd <ceiling>
```

`--safe-mode` is the one that matters; it drops all customization while leaving
subscription auth working. `--system-prompt` replaces the CLI's default agent prompt,
which is written for interactive coding and is worth about a thousand input tokens a call.

The cost cap in `docs/02` still works. `total_cost_usd` is reported even on a
subscription, so `run.spend_cents` remains meaningful shadow accounting, and
`--max-budget-usd` is a second hard ceiling the CLI enforces itself.

Rejected: prompting for JSON and parsing prose (the CLI can enforce a schema, so there is
no reason to); an in-process Python SDK on a subscription token (not a supported auth
path); running the CLI without the isolation flags (see the number above).

---

## Web layer

**FastAPI + Jinja2 + HTMX.** No build step, no npm, no bundler, no framework.

HTMX is the load-bearing choice, and it is what makes "server-rendered" compatible with
the write-back requirement in `docs/06-dashboard.md`. Resolving a commitment is:

```html
<button hx-post="/commitments/42/resolve"
        hx-target="closest .commitment"
        hx-swap="outerHTML">Resolve</button>
```

The endpoint returns the re-rendered card. No client state, no serialization layer, no API
you have to keep in sync with a frontend. For a seven-action dashboard used by one person,
this is the entire justification for not writing a SPA.

CSS is `design/tokens.css` plus hand-written rules. No Tailwind, no component library. The
design system is 300 lines of CSS and adding a framework would mean fighting it on every
rule in `design/design-system.md` §7.

Rejected: React or Svelte (a build step and a state layer for seven buttons), Streamlit
(cannot express the design system), plain Starlette (FastAPI's validation is worth the
small extra weight).

---

## Scheduling

**launchd on macOS**, one plist per job, each invoking the CLI.

```
com.cognifer.backglass.sync.plist     every 30 min
com.cognifer.backglass.plan.plist     05:45 local
com.cognifer.backglass.brief.plist    06:00 local
```

**Do not use an in-process scheduler** (APScheduler, `schedule`, a `while True` loop). It
dies silently with the process and you find out three days later when you notice the brief
stopped. The whole point of the Sources panel is catching that failure, and an in-process
scheduler makes the failure invisible to it.

If the laptop sleeps through 06:00 regularly, move the jobs to **GitHub Actions** on a
schedule with the SQLite file in a private repo or on object storage. Free, no hardware,
and the run log is the audit trail. Cron on a small VPS or Fly.io machine is the third
option if the data boundary decision permits it.

---

## Email delivery

**A transactional provider (Resend or Postmark), not the Gmail API.**

The Gmail connector holds `gmail.readonly` and `docs/08` forbids the system from ever
sending. Keeping send capability out of that credential entirely is worth an external
dependency, because it makes "this system cannot email anyone as me" true by construction
rather than by discipline.

Render the brief as inline-styled HTML per `docs/05-morning-brief.md`, with a plaintext
alternative generated from the same data rather than by stripping tags.

Rejected: SMTP through the owner's own account with an app password (works, but puts send
capability back in reach and lands in Spam more often than a proper provider).

---

## CLI

**Typer.** The whole system is operated from the command line and the surface is small:

```
backglass init                 create db, run migrations
backglass sync [--dry-run]     ingest + extract
backglass plan [--date]        generate a day plan
backglass brief [--send]       generate and optionally send
backglass status               connector health, spend, last run
backglass extract --reprocess --version N
```

`--dry-run` on `sync` is a hard requirement, not a convenience. It prints the diff and
writes nothing, and it is what makes every phase after the first debuggable.

---

## Testing

**pytest.** Three layers, and keeping them separate is the point:

**Unit tests** cover date resolution, capacity computation, dedup, entity resolution, and
the plan ordering rules. These are pure functions and should be exhaustive. Date
resolution across a UTC-7 to UTC+5:30 move gets its own file.

**Pipeline tests** run the full sync against recorded fixtures with the model client
mocked to return canned extraction responses. These test the post-processing in
`specs/extraction-prompts/extract-commitments.md` §Post-processing, not the model. The
idempotency assertion lives here: run twice against a frozen fixture, assert
`run.writes == 0` on the second pass.

**Evals** actually call the model against golden fixtures and report precision and recall
per prompt version. These do **not** run in CI, are not pass/fail, and exist so that
changing a prompt produces a number you can compare. Confusing an eval with a test is how
a nondeterministic system ends up with a flaky suite that people learn to ignore.

No test calls a live third-party API. Connector fixtures are recorded with `vcrpy`; model
responses are hand-written JSON so they stay readable.

---

## Tooling

```
ruff          lint + format, one tool, no Black/isort/flake8 stack
mypy          strict on backglass/, lenient on tests/
pytest        with -x and --tb=short by default
pre-commit    ruff + mypy + the palette validator
```

`scripts/validate-palette.mjs` runs in pre-commit and in CI. It exits non-zero, so a
color change that breaks contrast or CVD separation cannot land silently.

---

## Dependencies

Every runtime dependency and why it earned its place. The bar: a dep must replace
code that would be genuinely dangerous to hand-write, not merely tedious. What was
rejected is recorded so the argument does not have to be re-had.

| dep | why it earned its place | what rejecting it would have cost |
|---|---|---|
| typer | the whole CLI surface; argument parsing is a solved problem | hand-rolled argparse trees for 20+ commands |
| pydantic (+settings) | extraction schemas ARE the pydantic models; `--json-schema` is generated from them | a second, driftable schema definition |
| fastapi + uvicorn + jinja2 | the one-page dashboard; server-rendered, no build step | raw WSGI + string templates |
| python-multipart | FastAPI form posts (quick-add, people forms) | none — it is FastAPI's documented form dep |
| google-api-python-client + google-auth-oauthlib | Gmail/Calendar/Drive auth + discovery; OAuth token refresh is dangerous to hand-write | re-implementing OAuth refresh, the classic security foot-gun |
| pypdf | drop-folder + Drive PDF text (docs/12 ruling: BSD-3, pure Python; PyMuPDF rejected on AGPL + 50 MB, pdfplumber as overkill) | "PDF support" that UTF-8-decodes binary noise |

Dev-only: pytest, ruff, mypy, vcrpy (cassettes recorded at activation, docs/13).

Deliberately vendored rather than depended on: quote/signature stripping regexes
(talon, email-reply-parser — both unmaintained since ≤2022, Apache-2.0/MIT permit
vendoring with attribution, in `extract/quoting.py`) and the iMessage typedstream
text scan (`connectors/_typedstream.py`, technique from imessage_tools /
imessage-exporter's format documentation). A dependency that no longer ships
releases is a supply-chain liability wearing a convenience costume.

---

## Layout

```
backglass/
  __main__.py            typer app
  config.py              env + settings, pydantic-settings
  db/
    __init__.py          connection, row factory, pragmas
    migrations/          numbered .sql
    queries/             named .sql, loaded by helper
  connectors/
    base.py              the Protocol from docs/07
    gmail.py  drive.py  notes.py  calendar.py  canvas.py
    boundary.py          docs/08 enforcement — runs before persistence
  extract/
    triage.py  commitments.py  goal_signal.py
    prompts.py            loads specs/extraction-prompts/*.md
    schemas.py            pydantic models -> tool JSON Schema
  plan/
    capacity.py  planner.py  rollover.py
  goals/
    targets.py  checkpoints.py  staleness.py  risk.py
  brief/
    daily.py  monday.py  friday.py  render.py
  web/
    app.py  routes/  templates/  static/
```

`connectors/boundary.py` sits in the connector package deliberately. Putting it in a
filter or a middleware invites someone to bypass it; being the thing a connector calls
before it yields makes bypassing it a visible edit.

---

## Deployment

Runs on the owner's Mac to start. The database is one file; back it up by copying it.

When the laptop-sleeping problem gets annoying, the migration path is GitHub Actions for
the scheduled jobs and a tunnel or a small Fly machine for the dashboard. Nothing in the
stack assumes a local filesystem except the Obsidian connector, which is the one piece
that would need rethinking if the pipeline moves off the machine holding the vault.

Do not plan for that move now. Note it and keep the connector interface clean.
