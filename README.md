# Backglass

A personal commitment ledger that reads your email, documents, notes, and calendar, and
gives you back a morning brief and a dashboard.

Single user, runs on your machine. MIT-licensed. There is no hosted service and no
account: the ledger, the database, and the dashboard never leave your computer.

Extraction does call a model. Bring your own Anthropic or DeepInfra API key (or run it
free against your existing Claude subscription via the Claude Code CLI) — and understand
that the text of every item passing the local rule filter is sent to whichever backend you
configure: up to 2,000 characters at triage, and the full body at extraction. Read
[docs/08-privacy-and-data-boundary.md](docs/08-privacy-and-data-boundary.md) before you
point this at a mailbox carrying client correspondence.

> **back·glass** *(n.)* — the illuminated art panel standing at the rear of a pinball
> machine, carrying the score reels. The one surface that tells you, at a glance and
> without being asked, exactly where you stand.

The name is the product thesis. Everything here exists to make one lit panel that answers
"where do I stand" without you having to go and look.

## What it does

- Reads Gmail, Google Drive, a notes app, and your calendar on a schedule.
- Extracts typed records from what it reads: commitments you made, commitments owed to
  you, deadlines, and goal progress.
- Plans your day against actual available capacity.
- Sends a brief at 06:00 you can read in two minutes.
- Serves a dashboard you keep open.

## What it deliberately does not do

No semantic search, no chat interface, no vector database. The queries are known in
advance, so the work happens once at ingest rather than repeatedly at query time. See
`docs/02-architecture.md` for why this matters.

No automatic calendar writes. It proposes; you accept.

## Getting started

See [`GETTING_STARTED.md`](GETTING_STARTED.md) for the full sequence — clone, install,
configure a model backend, authorize your own sources, and run it.

## Layout

```
CLAUDE.md      read this first if you are an agent working on this repo
docs/          product and architecture decisions, numbered in reading order
design/        design system, tokens, and a rendered preview
specs/         schema DDL, extraction prompts, API contracts, roadmap presets
scripts/       utilities, including the release manifest and palette validator
```

## Status

The core pipeline (ledger, extraction, morning brief, dashboard, schedule/goal engine)
is built and covered by its own test suite. `docs/09-build-plan.md` has the original
build sequence if you want the reasoning behind the order.

## License

MIT — see [`LICENSE`](LICENSE).
