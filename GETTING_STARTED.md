# Getting started

Backglass runs entirely on your machine — no account, no hosted service. This is the
full path from a fresh clone to a serving dashboard reading your own sources.

## 1. Install and configure

```bash
git clone <repo> && cd backglass
uv sync                              # installs Python deps
cp .env.example .env                 # set ANTHROPIC_API_KEY (or MODEL_BACKEND=claude_cli/deepinfra)
uv run backglass init                # runs migrations
uv run backglass setup               # auto-detects local sources, writes remaining .env
uv run backglass auth gmail          # OAuth — needs your own Google Cloud OAuth client (see below)
uv run backglass sync
uv run backglass dashboard
uv run backglass schedule install    # optional: launchd jobs for automatic sync/brief
```

A few of those steps need one more thing from you before they'll do anything useful:

- **A model backend.** If you already have a Claude subscription, the cheapest path is
  `MODEL_BACKEND=claude_cli` with the Claude Code CLI installed — extraction then costs
  nothing per run and no API key ever goes in a file. Otherwise `.env.example`'s default
  `MODEL_BACKEND=anthropic` brings your own key (`ANTHROPIC_API_KEY` or `MODEL_API_KEY`)
  and bills per item; the monthly spend cap in `.env` is enforced in code and degrades to
  triage-only rather than overspending. `docs/10-tech-stack.md` §Model backends has the
  full tradeoff.
- **`backglass setup`** only finds *local* sources it can detect on disk (Apple Notes,
  Reminders, Messages, Anki, Avorio, Obsidian) — it writes their `.env` lines for you
  and tells you what's still missing. It cannot detect Gmail/Calendar/Drive on its own,
  because there is nothing on disk to find; that needs the OAuth step below.
- **Gmail/Calendar/Drive need a Google Cloud OAuth client first** — `docs/07-connectors.md`
  §Setting up Gmail/Calendar/Drive OAuth walks through creating one. Once
  `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are in `.env`, also add one label per mailbox
  you want read, e.g. `GMAIL_ACCOUNTS=personal` (the account label is up to you — it's
  just how the credential gets named). Only then does
  `uv run backglass auth <label> --source gmail` (the line above uses `gmail` as both the
  label and the source, which works, but a more memorable label like `personal` reads
  better once you have more than one account) have anything to store, and only labels
  listed in `GMAIL_ACCOUNTS`/`CALENDAR_ACCOUNTS`/`DRIVE_ACCOUNTS` are ever read by sync.
- **`backglass schedule install`** is optional and macOS-only (launchd). Skip it and run
  `backglass sync` / `backglass brief` by hand if you're on another OS or just trying it
  out first.

Run `uv run backglass doctor` at any point — it reports exactly what's missing (API key,
OAuth client, unauthorized accounts, launchd jobs) with a clear message and a non-zero
exit, never a stack trace.

## 2. Optional: build the Mac app

Everything above is the CLI plus a local web page, which is the whole product. If you'd
rather have it in the Dock, `desktop/` is a Tauri shell over the same dashboard:

```bash
cd desktop && npm install && ./build-sidecar.sh
# → desktop/src-tauri/target/release/bundle/macos/Backglass.app
```

The build freezes the Python backend with PyInstaller and bundles it inside the `.app`,
so the result runs with this repo deleted. Its data lives in
`~/Library/Application Support/Backglass` — a fresh, empty ledger, not the one you were
using from the checkout. Point `DB_PATH` at your existing database if you want both to
read the same one.

**The app is unsigned.** Nobody here is paying Apple's developer program, so macOS
Gatekeeper will refuse it on first launch — including for you. Right-click the app and
choose *Open*, then confirm; macOS remembers the decision. If you send the `.app` to
someone else it arrives quarantined, and they need the same right-click → *Open* (or
`xattr -dr com.apple.quarantine /Applications/Backglass.app`). Anyone uncomfortable with
that should build it themselves from source, which is the point of this repo being open.

## 3. Try a roadmap preset

The goal engine ships with seven fictional starting-point roadmaps so you can see the
day planner and goal tracking work before wiring up anything real:

```bash
uv run backglass roadmap paths                 # list the presets: founder, app-launch,
                                                # pm, swe, ship-blocked-product,
                                                # company-revenue, medical
uv run backglass roadmap start founder          # short interview, then instantiates it
```

Each preset is entirely made up — no real dates, no real people — and exists to
demonstrate the roadmap → target → checkpoint → risk pipeline end to end.

## 4. What each command actually does

| command | what it does |
|---|---|
| `backglass init` | Creates the SQLite database and runs migrations. Safe to run repeatedly. |
| `backglass setup` | Detects local sources on this machine and writes their `.env` lines. Idempotent. |
| `backglass auth <label> --source <gmail\|calendar\|drive>` | Runs the Google OAuth consent flow once per account, stores read-only tokens. |
| `backglass sync` | Ingests, triages, and extracts everything new since the last run. Degrades per source on failure; never blocks on one bad connector. |
| `backglass brief --send` | Generates and emails the morning brief (needs `RESEND_API_KEY` + `BRIEF_TO`/`BRIEF_FROM`). |
| `backglass dashboard` | Serves the local dashboard at `http://127.0.0.1:8765`. |
| `backglass doctor` | Preflight check: one line per requirement, non-zero exit on any failure. |
| `backglass schedule install [--dry-run]` | Renders and installs the launchd jobs for automatic sync/plan/brief/shutdown. |

That's the whole surface for a first run. `docs/11-ux-flows.md` walks through the nine
end-to-end flows (morning brief, dashboard triage, goal check-in, and so on) once you
have real data flowing.
