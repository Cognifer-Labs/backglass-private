# Backglass

A personal commitment ledger that reads your email, documents, notes, and calendar, and
gives you back a morning brief and a dashboard.

Single user. Not a SaaS, not yet.

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

```bash
cp .env.example .env      # fill in credentials
python -m backglass.db init
python -m backglass.sync --dry-run
```

`--dry-run` prints the diff it would apply and writes nothing. Use it until the output
looks right.

## Layout

```
CLAUDE.md      read this first if you are an agent working on this repo
PROMPT.md      copy-paste kickoff prompts for Claude Code, one per phase
docs/          product and architecture decisions, numbered in reading order
design/        design system, tokens, and a rendered preview
specs/         schema DDL, extraction prompts, API contracts
scripts/       utilities, including the palette validator
```

## Status

Pre-phase-0. Nothing is built. `docs/09-build-plan.md` has the sequence, and phase 0
involves no code at all — read it before writing any.
