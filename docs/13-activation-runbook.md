# Activation Runbook

The ordered path from "all tests green" to "running against real accounts". Run
`backglass doctor` after every step — it prints one line per check and exits
non-zero while anything is missing. The automation half of activated is a clean
doctor; the product half is the seven-day soak at the end.

Owner-at-the-keyboard steps are marked **[you]**; everything else is a command.

## 0. `backglass setup`

One command finds every local store on the machine (Anki, Avorio, Messages,
Obsidian, the Apple bridges) and writes the `.env` lines you would otherwise hunt
for; token and OAuth sources get their exact remaining step printed. Run it first,
run it again any time — it is idempotent and read-only until you confirm a write.
`--yes` takes every find; `--reviews-target <id>` binds review streaks to a goal.

## 1. Clean slate

- [ ] Quit Backglass.app if running.
- [ ] `rm data/backglass.db*` — the current db is **seeded demo data**
      (fake commitments, fake people). Nothing real is lost.
- [ ] `backglass init`

## 2. Google OAuth client  **[you]**

- [ ] console.cloud.google.com → new project "Backglass" → OAuth consent screen
      (internal/testing, your two accounts as test users) → Credentials →
      OAuth client ID, type **Desktop app**.
- [ ] Enable the Gmail API, Calendar API, and Drive API on the project.
- [ ] Copy client ID + secret into `.env`.

Scopes requested (read-only, nothing else — docs/08): `gmail.readonly`,
`calendar.readonly`, `drive.readonly`.

## 3. `.env`

- [ ] `cp .env.example .env` (or edit the existing one) and set at minimum:
      `OWNER_NAME`, `OWNER_EMAILS`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
      `BRIEF_TO`, `BRIEF_FROM`, `RESEND_API_KEY`.
- [ ] Optional sources as wanted: `GITHUB_TOKEN`, `SLACK_TOKEN`+`SLACK_CHANNELS`,
      `IMESSAGE_DB_PATH`, `INBOX_FOLDER_PATH`, `OBSIDIAN_VAULT_PATH`,
      `APPLE_NOTES=1`, `APPLE_REMINDERS=1`, `CANVAS_BASE_URL`+`CANVAS_TOKEN`.
- [ ] Optional privacy screen: `APPLE_TRIAGE=1` (step 5 creates the shortcut).
- [ ] If you set `INBOX_FOLDER_PATH`, keep anything *derived* from those files — text
      extractions especially — in a sibling folder, not inside the drop root. The
      connector reads `.txt`, so a text twin beside its PDF ingests the same document
      twice and pays extraction for both (docs/07 §The drop folder in practice).

## 4. OAuth runs

- [ ] `backglass auth personal --source gmail`
- [ ] `backglass auth asu --source gmail`
- [ ] `backglass auth personal --source calendar`
- [ ] `backglass auth personal --source drive`

Each opens a browser consent screen once; tokens land in the `credential` table.

## 5. macOS grants  **[you]**

- [ ] **Full Disk Access** for your terminal (and later the app) if
      `IMESSAGE_DB_PATH` is set: System Settings → Privacy & Security → FDA.
- [ ] **Automation** prompts appear on the first Notes/Reminders sync — approve.
- [ ] If `APPLE_TRIAGE=1`: Shortcuts.app → new shortcut named **Backglass Triage**
      with exactly three actions: *Receive Text input* → *Use Model* (model:
      Private Cloud Compute, prompt: Shortcut Input) → *Stop and output* (Model
      Response).

## 6. First sync

- [ ] `backglass sync --dry-run` — read what it would write. The first run is a
      full backfill; expect minutes, and expect Slack to crawl slowly if
      configured (new-app rate tiers).
- [ ] `backglass sync`
- [ ] `backglass status` and `backglass commitments` — the Phase 1 acceptance
      question: are these your actual open commitments?
- [ ] `backglass commitments --review` — triage the review queue on the
      dashboard (`backglass dashboard`, or the app).

## 7. Record real cassettes

Closes the Phase 1 open box (tasks/todo.md §Session 2): with credentials now
live, record vcrpy cassettes for gmail/calendar/drive fixtures so the
hand-written fakes get real-shaped counterparts. Keep the rule: recorded once,
scrubbed of content, tests still never touch the network.

## 8. Scheduling

- [ ] Install the launchd jobs per `launchd/README.md` (05:45 plan, login plan
      catch-up, 06:00 brief, 30-minute sync).
- [ ] `backglass doctor` — the launchd check flips green.

## 9. The app

- [ ] `bash desktop/build-sidecar.sh` (or use the dev shell during the soak).
- [ ] Copy `Backglass.app` to /Applications. First launch creates
      `~/Library/Application Support/Backglass/`; copy your `.env` there if you
      run the packaged app (the repo checkout keeps its own).

## 10. The seven-day soak

The three open phase-exit criteria are calendar time, not code. Track here:

| day | brief correct? | all sources healthy? | kill rate ≥ 85%? |
|---|---|---|---|
| 1 |  |  |  |
| 2 |  |  |  |
| 3 |  |  |  |
| 4 |  |  |  |
| 5 |  |  |  |
| 6 |  |  |  |
| 7 |  |  |  |

- **Phase 1 closes** when the commitments query stays correct against real mail.
- **Phase 2 closes** after seven consecutive briefs with no wrong claim
  (docs/09 §Phase 2).
- **Phase 5 closes** after seven days of every source healthy and the triage
  kill rate at or above 85 percent.

A wrong brief claim during the soak is a bug report: file it in tasks/todo.md
with the source item id, fix, and restart the seven-day counter for Phase 2.

## Optional: batch mode (−50% on extraction)

Once the soak is stable and an Anthropic API key exists (`MODEL_BACKEND=anthropic`,
key in `MODEL_API_KEY` or `ANTHROPIC_API_KEY`), extraction can move to the Message
Batches API at half price:

1. `backglass schedule install` renders and loads the batch-submit/batch-collect
   templates along with the rest (submit 22:00, collect 05:30 — before plan and brief).
2. Verify once by hand: `backglass batch submit`, then `backglass batch collect` the
   next morning; `backglass batch status` shows the ledger.
3. Failure policy is automatic: an expired or errored batch leaves its items pending
   and the next `backglass sync` extracts them synchronously at full price. `doctor`
   prints a note (never a failure) if a batch has been outstanding for more than 26h.
