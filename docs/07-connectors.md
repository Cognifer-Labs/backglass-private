# Connectors

Every connector implements the same interface and writes only `source_item` rows. None of
them know about commitments, goals, or plans.

```python
class Connector(Protocol):
    name: str

    def health(self) -> Health: ...
    def fetch(self, since: Cursor) -> Iterator[SourceItem]: ...
```

`since` is a per-connector opaque cursor stored in the credential row. Never re-scan from
zero on a routine run.

## Gmail

- Gmail API, incremental via `historyId`. Full scan only on first run or cursor loss.
- Scope: `gmail.readonly`. Nothing else. The system never sends or modifies.
- Thread-aware: quoted history is stripped before hashing, so a forty-message thread does
  not produce forty near-identical items.
- Respect the data boundary in `docs/08-privacy-and-data-boundary.md` **before** the item
  reaches the extractor, not after.

## Google Drive

- Drive API changes feed, same cursor pattern.
- Text extracted from Docs, Sheets, and PDFs. Binary and media skipped.
- Only files owned by or explicitly shared with the owner. Do not crawl shared drives.

## Notes

Unresolved: which app. This changes the connector meaningfully.

| option | cost | notes |
|---|---|---|
| Obsidian | lowest by a wide margin | markdown files on disk, watch the vault directory, no API, no auth |
| Notion | moderate | clean API, but each page must be explicitly shared with the integration, which is the usual first-run stumble |
| Apple Notes | highest | no public API, needs local scripting on a Mac, fragile across OS updates |

**Recommendation: Obsidian.** Roughly a tenth the work of the alternatives. If the owner
has not committed to a notes app yet, this decision is effectively free.

## Instagram

Friend plans live in DMs. There is no official API for a personal account's inbox (the
Basic Display API died December 2024; the Graph API reads only professional accounts),
so this connector has two lanes behind one allowlist:

- **Export lane** (`instagram`, the default): the owner periodically requests Meta's
  "Download Your Information" export in JSON format and points `INSTAGRAM_EXPORT_PATH`
  at the unzipped folder. Official, no credential, zero ban risk; data is as fresh as
  the last export. Cursor is the highest `timestamp_ms` seen, so re-dropping a newer
  export on the same path yields only what is new.
- **Live lane** (`instagram:live`, experimental): instagrapi against the private mobile
  API. Fresh every run, but it violates Instagram ToS and can get the account
  checkpointed or banned. Enabled only when both `INSTAGRAM_USERNAME` and
  `INSTAGRAM_SESSION_FILE` are set; the session file is created once so routine runs
  never perform a fresh login.

### Turning the live lane on

```bash
uv sync --extra instagram          # instagrapi is an extra, never a dependency
uv run backglass instagram login   # asks for the password once, interactively
uv run backglass instagram chats   # lists thread titles for INSTAGRAM_CHATS
```

`login` exists to make the one controllable safety factor easy. Instagram tolerates a
client that reuses a session and reacts badly to one that keeps re-authenticating, so the
password is asked for exactly once, at a hidden prompt — never in `.env`, never in an
argument a shell history would keep, never held after the session is written. The session
file gets `.env`'s treatment: mode 0600, set *before* the session lands in it.

None of that makes the lane sanctioned. It drives an interface Meta does not publish and
its terms do not permit, and the realistic downside is a checkpointed or banned account
rather than a broken connector. The trade is the owner's; these commands only make the
safer half of it easy. The allowlist still gates everything — see below.

**Allowlist, not inbox.** Only named group-chat titles and people are read by either
lane — the Slack rule again: a personal tool reads the handful of threads the owner
names. The list lives in `monitored_chat` under the shared source `instagram` (see
§Monitored conversations): both lanes report every thread they see as a sighting —
the export lane from a full scan of the export, the live lane from the threads it
lists without fetching unallowed ones — and the /chats page is where each becomes
`monitor` or `ignore`. One decision governs both lanes, and saying yes rewinds both
lanes' cursors so the history is read too. `INSTAGRAM_CHATS` seeds the table on the
first run, after which the page is the only thing that matters. Everything not chosen
is counted (`allowlist` rule) and never stored.

**Friend plans ride low.** Commitments whose evidence came from Instagram are demoted
out of Slipping/Awaiting into a last-priority "Friend plans" brief section — unless the
text names a birthday or special event (deterministic keyword test in `brief/daily.py`),
in which case they keep full priority. Category is derived from provenance at query
time; the commitment table is untouched.

## Calendar

- Google Calendar API for personal, Microsoft Graph for the work tenant if enabled.
- Read-only. The planner proposes; it never writes.
- Declined events are excluded from capacity. Tentative events count as busy.
- Events tagged as travel get their own handling in the capacity model, per
  `docs/04-daily-schedule-and-goals.md` §1.2.

## Canvas

Optional and last. The original request that seeded this project.

- Canvas REST API with a personal access token, `Authorization: Bearer`.
- `GET /api/v1/courses?enrollment_state=active&per_page=100`
- `GET /api/v1/courses/:id/assignments?include[]=submission&per_page=100`
- Follow RFC 5988 `Link` header pagination to completion.
- Leaky-bucket quota: read `X-Rate-Limit-Remaining`, back off on 403. Serialize requests;
  a one-at-a-time client is unlikely to be throttled, which is a good reason not to
  parallelize.
- Some institutions disable student-generated tokens. Check
  Account → Settings → Approved Integrations before building. If absent, the fallback is
  the ICS feed, which loses submission state.

### The fallback, built 2026-08-11 because ASU is one of those institutions

Approved Integrations renders "+ New Access Token" inert: *"Your Canvas administrators
have chosen to limit your ability to generate your own access token."* So
`connectors/canvas_ics.py` reads the per-user feed Canvas publishes at
Calendar → Calendar Feed. That document is handed out by the institution; this is the
fallback the paragraph above names, not a way around the decision it describes.

It emits `canvas:ics` rather than letting `apple_calendar` collect the subscription,
because tier 0 drops calendar invites by rule — the 200 `calendar:asu` rows are all kept
and none extracted. Read as a calendar, an assignment feeds the capacity model and never
becomes a commitment, so it lands on the schedule and never in the brief or the due-today
list.

**What it cannot do.** The feed carries no submission state, so the API connector's filter
on submitted-or-graded work has nothing to read: everything already handed in keeps
reading as an open obligation until closed by hand. `CANVAS_TOKEN` therefore wins whenever
it is set, and `_all_connectors` builds the two on an `elif` — running both would ingest
every assignment twice under two source names.

**Why it has no watermark, corrected 2026-08-20.** It shipped with one, on the due date,
and that cost the owner a semester of coursework. A due date is not monotonic with
publication: the feed is a single document that gains assignments due *before* the
furthest date already stored. The read on 2026-08-11 parked the cursor at 2026-09-04; the
read on 08-17 saw one course publish "Excused Absence Requests" due 2027-03-07 and parked
there; every read after emitted nothing, because no real coursework is due after March.
157 assignments upstream, 6 in the ledger, `status = ok` throughout. `canvas.py` is not
affected — it watermarks on Canvas's `updated_at`, which does move forward.

So `fetch` ignores `since` and emits every assignment the feed publishes. The whole
document is downloaded either way, and an unchanged assignment costs zero writes because
`ledger.upsert_source_item` short-circuits on `content_hash` before any model runs. The
cursor now records the instant of the read — the one monotonic fact available — and
nothing reads it back as a bound. `doctor`'s `canvas:ics fully ingested` check, which
compares `upstream_count()` against the ledger, is what named this and is the check to
read first if the feed ever looks quiet again.

### What the feed does not carry, and the 2026-08-21 archive read

The ICS feed is a list of dated obligations. It is not the course: it has no syllabus, no
course schedule, no page, no file, and no room or instructor. Everything a student
actually reads lives behind the Canvas UI, and on this installation there is no token to
reach it with.

So on 2026-08-21 the eight Fall-C shells were read **by hand, once**, through the
already-logged-in Safari session — Canvas's own API called from a canvas.asu.edu tab, GET
only, no scraping and no borrowed token, the same boundary the paragraphs above draw. The
result is a folder of documents (`~/Documents/ASU Fall 2026`), not a connector. It is
recorded here because it is a source of ledger evidence and this file is the place a
source is audited, and because the next person will otherwise try the paths that do not
work: the Files API is 403 on seven of the eight shells (the Files tab is disabled per
course), `/pages` is 404, `content_exports` returns a 22-byte empty zip for exactly those
shells, `public_url` is 404 for a student, and `/login/session_token` is 401 without a
token. What works is per-file `GET /api/v1/courses/:id/files/:id` in the live session.
`tasks/lessons.md` 2026-08-21 carries the full account, including why the browser will not
simply download the files for you.

**A repeat is a hand operation, deliberately.** Automating it would mean holding a
session, which is the thing this project declined to do when the token was refused. What
keeps the archive current instead is that a syllabus changes about once a semester, and
the schedule that matters is already in the ledger through the feed.

## Local and later-phase sources

The sections above are the network sources the first phases were built around. These
were added after, share the same `Connector` protocol, and are gated the same way — one
env key, no key means the connector is never constructed. They are listed here because
the protocol is the contract: a source that is not in this file is a source nobody
audits.

| source name | gate | cursor | boundary | how it fails |
|---|---|---|---|---|
| `imessage` | `IMESSAGE_DB_PATH` + `IMESSAGE_CHATS` | max `message.ROWID` | yes — handles are addresses | without Full Disk Access the file still stats — only the *open* is refused, with `unable to open database file` rather than anything that says "denied" |
| `apple-mail` | `APPLE_MAIL_PATH` | `date_received`, not ROWID — see below | yes, and this is the source docs/08 was written about | same Full Disk Access refusal as iMessage; `health()` opens the Envelope Index rather than trusting that the directory is visible |
| `apple-notes` | `APPLE_NOTES=1` | modification-date watermark | yes — note bodies carry addresses | Automation permission denied, reported by `health()` |
| `calendar:apple` | `APPLE_CALENDAR=1` | none — a bounded window, re-read each run | yes — titles and locations can carry addresses | Automation permission denied, reported by `health()` |
| `reminders` | `APPLE_REMINDERS=1` | fetch-window watermark (no mtime exists) | yes | same Automation prompt as Notes |
| `apple-contacts` | `APPLE_CONTACTS=1` | none — reference data, re-read each run | yes — a card is a name and an address | same Automation prompt as Notes |
| `files` | `INBOX_FOLDER_PATH` | mtime watermark | yes | unsupported file types are counted and reported, never silently skipped |
| `github` | `GITHUB_TOKEN` | two watermarks in one string: search time + notifications `Last-Modified` | yes | 401 on a revoked token; the search quota is per-minute, so requests stay serialized |
| `anki` | `ANKI_DB_PATH` | revlog row range | none — tallies carry no addresses and no card text | Anki holding the write lock past the busy timeout degrades the source for one cycle |
| `avorio` | `AVORIO_DB_PATH` | `MAX(reviews.reviewed_at)` | none, same reason | schema drift; `health()` verifies every required table and column, not just the file |

`apple-contacts` is the one row in this table that is **not** a `Connector`. It has no
cursor, yields no `SourceItem`, and never writes to `source_item` — a contact is not an
event and carries no commitment, so it is reference data, and it lands in `entity` as
identifiers on people the ledger already knows (`backglass/contacts.py`). It is listed
here anyway because it reads the owner's data behind the same Automation permission,
which is exactly the thing this file exists to make auditable: it is boundary-checked
before anything is persisted, health-checked by `doctor`, and it runs from `sync` under
the same rule 5 wrapper as every connector. What it buys is the Conversations page: the
consent prompt used to ask whether to read `+14802411748`, which is not a question
anybody can answer.

### The drop folder in practice, live since 2026-08-21

`INBOX_FOLDER_PATH` had never been set. It now points at `~/Documents/ASU Fall 2026`, the
Canvas archive described under Canvas above, and the first real drop folder taught three
things worth writing down:

- **Derived text belongs outside the drop root.** The connector reads `.txt`, so a
  plain-text extraction sitting beside its own PDF is the same syllabus ingested twice,
  triaged twice and extracted twice. The extractions live in a sibling folder,
  `ASU Fall 2026 — extracted text`, and the archive's own README says why so nobody
  helpfully moves them back.
- **`MAX_BYTES` went 512 KB → 4 MB.** Seven of the archive's documents were over the old
  ceiling and every one of them was a document — a 784 KB lab syllabus, a 2.8 MB
  recitation activity, a 2.3 MB textbook chapter. They are large because they are typeset
  and full of figures, not because they are dumps; the comment above the constant carries
  the same reasoning. `MAX_CHARS` is what bounds extraction cost, so the only thing this
  number ever protected was memory.
- **Triage does the curation.** Run 521 ingested 29 files, kept 4 — both CHM 113 lecture
  documents, the recitation activity, the HON 171 syllabus — and dropped 25 readings,
  worksheets and transcripts. Extraction wrote 12 commitments from those 4, including two
  HON 171 paper deadlines that existed in no other source. Unsupported types (.docx,
  .xlsx, images, .svg) are counted and reported rather than silently skipped, which is
  the rule the whole table above is built on.

### Two things a calendar write does not put in the ledger

Recorded here because both were learned by writing 28 syllabus-derived dates to a new
Apple Calendar and then finding 13 of them in the ledger:

- **All-day events never ingest.** `apple_calendar._to_item` returns `None` for them by
  design — an all-day row is not capacity — so a drop deadline, a no-class day or an
  all-day reminder can live on the owner's calendar and be invisible to every Backglass
  surface. Nothing fails; the row simply is not there.
- **The window is 21 days ahead, 7 behind.** A timed exam in November is not in the ledger
  in August. It arrives when it comes inside the window.

The durable record of a dated obligation is therefore the commitment extraction made from
the document, not the calendar event — which is the ledger-primary rule in CLAUDE.md,
arrived at from the other direction.

Two rules those local stores exist to teach:

- **Open a live SQLite store `mode=ro` with a busy timeout, never `immutable=1`.** All
  three of these stores are WAL; `immutable` makes SQLite skip the `-wal` file, so a read
  is either silently stale or fails with "no such table" while the app is open.
- **An `external_id` must name content that cannot be recomputed differently.** Key a
  batch item to the exact immutable row range it summarizes. A high-watermark id
  (`reviews:<day>:<max-id>`) re-emits the same id with different content after a rescan,
  which the 0002 immutability trigger turns into a failed-looking sync.

## Choosing what is read

`monitored_chat` holds the answer, and `/chats` is where it is given. A conversation gets
a row the first time a connector sees it, `decision` NULL, which does not mean "off" — it
means *seen and not yet decided*, and that is what raises the prompt on the dashboard and
lists it on the page. Nothing is ever monitored by default: consent to read one group says
nothing about the next.

`ignore` is stored rather than treated as absence. Without it every sync would re-raise
every conversation the owner has already declined, and a prompt that repeats itself is one
people stop reading.

Connectors report what they saw and never write it — the same seam `excluded_by_rule` uses.
`sync` records the sightings, which is also why a connector with nothing monitored is
*unhealthy but not inert*: it still discovers conversations, or the page would have nothing
to offer and there would be no way to start.

`IMESSAGE_CHATS` / `INSTAGRAM_CHATS` still work and are seeded into the table as `monitor`
on the first run. A click beats a stale setting: re-seeding never overrules an `ignore`.

## iMessage reads named conversations only

`IMESSAGE_CHATS` is an allowlist, exactly like Instagram's, and for the same reason: an
inbox-wide read of a personal message store captures mostly other people's words about
things the owner never meant to file. Empty means the connector reports **unhealthy**, not
"read everything" — the safe direction. Groups are named by display name, one-to-one
threads by the other party's handle; `backglass imessage chats` lists both with counts.

`IMESSAGE_LOOKBACK_DAYS` (default 90) bounds how far back a scan reaches. The cursor
already stops the connector re-reading what it has seen; the window is what stops the
*first* run walking an entire archive — 42,879 messages on the owner's machine, where the
useful span for a commitment ledger is the last few months. It is applied in SQL, and the
column is normalised from Apple's nanoseconds to seconds first: older stores and
third-party exports hold seconds, and comparing those raw against a nanosecond threshold
silently drops every legacy row.

`backglass imessage prune` reaches backwards. Narrowing the allowlist otherwise only ever
applies to messages not yet read, leaving everything captured under the looser rule in
place — so the prune goes through the same delete gate the docs/08 boundary purge uses,
since it is the same decision: content the owner has determined this system may not hold.
Dry run unless `--apply`.

## Calendar without Google

`calendar:apple` (`backglass/connectors/apple_calendar.py`) reads Calendar.app through
the same automation bridge as Notes and Reminders. That matters more than it sounds: Calendar.app already holds whatever accounts
macOS syncs — Google ones included — so on a Mac that has signed into its calendars, real
events reach the day planner with **no Google Cloud project, no OAuth client, no consent
flow and no Full Disk Access**. `APPLE_CALENDAR=1` is the whole setup.

The Google connector above is still the right answer for an account macOS does not have,
or for a machine where Calendar.app is not configured. Where both are on, they will
produce the same meeting twice under different ids; run one.

Two behaviours worth knowing:

- **It deduplicates across calendars.** The same event commonly sits in more than one
  local calendar with different UIDs, and undeduplicated the planner subtracts it from
  the day twice. Identity is (title, start, end), and the survivor is chosen by sorting
  so the ledger does not churn between two spellings of one event.
- **`APPLE_CALENDAR_SKIP`** drops calendars by name. Subscribed holiday and birthday
  feeds are the reason it exists: they are all-day events, so they are excluded from
  capacity anyway, but naming them keeps the source list honest.

## Mail without Google

`apple-mail` (`backglass/connectors/apple_mail.py`) is the same realisation as
`calendar:apple`, applied to the largest source of a person's commitments. Mail.app holds
the messages macOS already syncs — both Gmail accounts and the iCloud one, on this
machine — so mail reaches the ledger with **no Google Cloud project, no OAuth client and
no consent flow**. It does need Full Disk Access, because `~/Library/Mail` is
TCC-protected the way the Messages store is.

The Gmail connector above is still right for an account this Mac does not have. Running
both against the same account produces every message twice under different ids; run one.

Three things it does that are worth knowing before changing it:

- **The index selects; the files carry the message.** `MailData/Envelope Index` is a
  SQLite database of one row per message per mailbox, used only to decide which messages
  a run has not read. Everything stored — sender, subject, body, and the `Date` offset
  rule 4 depends on — is parsed from the `.emlx` file, so there is one source of truth
  per message.
- **The cursor is `date_received`, not `ROWID`.** Mail rebuilds the Envelope Index after
  a crash or an upgrade, and a rebuild renumbers every row. A ROWID watermark would then
  either re-read the whole store or, far worse, sit above rows now numbered beneath it
  and skip mail permanently. The window is inclusive of the watermark, so the boundary
  second is re-read each run and `content_hash` makes that free.
- **`external_id` is the RFC822 `Message-ID`.** A Gmail account exposes `INBOX` and
  `[Gmail]/All Mail` as separate folders holding the same message: two index rows, two
  files, one commitment. Junk, Spam, Trash, Deleted Messages and Drafts are not read at
  all — a draft is something the owner has not said yet.

`APPLE_MAIL_PATH` points at `~/Library/Mail`, the parent, not at the version directory
inside it. The connector resolves `V10` (and `V11` after the next macOS upgrade) itself,
because a pinned version turns an OS update into a source that silently stops collecting.

## Turning on the messaging sources

Three connectors read the places plans actually get made — and all three need something
only the owner can give them, so they ship built and switched off. `backglass setup`
reports the state of each; it now proves a local store by opening it, so "configured"
means the next sync will read it rather than that a file is on disk.

**iMessage.** Grant Full Disk Access in System Settings → Privacy & Security. Add both
the terminal and the `uv` binary: launchd jobs run through `uv`, and TCC grants are
per-binary, so approving only the terminal leaves every scheduled sync failing while
manual runs work. Then `backglass setup` writes `IMESSAGE_DB_PATH`.

**Instagram.** Request a data export in JSON (not HTML) at `accountscenter.instagram.com`
and unzip it into `~/Downloads`; detection finds any `instagram-*` folder containing a
`messages/inbox`. The first sync discovers every thread in it; choose what to read on
the /chats page — the connector is allowlist-only by design, because a DM archive is
the least filtered thing the owner owns, and nothing is read until it is chosen.
A live lane exists (`INSTAGRAM_SESSION_FILE`) and is experimental.

**Slack.** A user token in `SLACK_TOKEN` plus the channel ids in `SLACK_CHANNELS`. Named
channels only; there is no inbox discovery, for the same reason Instagram is allowlisted.

### Platforms with no connector, and why

Not everything is reachable on defensible terms, and this is the honest split rather than
a roadmap:

- **WhatsApp, Signal** — live traffic is end-to-end encrypted with no supported local
  read. WhatsApp offers a per-chat export; that would be a file-parsing job of the same
  shape as the Instagram export lane, and nobody has written it.
- **LinkedIn, X** — scraping is against their terms and the APIs are closed or paywalled.
- **Discord, Telegram** — Discord's personal DMs are unreachable without a user token,
  which its terms forbid; Telegram has an official user-level API and no connector yet.

Those platforms still reach the ledger, because the things that matter from them arrive
as mail: LinkedIn invitations, Meetup RSVPs, Eventbrite tickets, Facebook event notices.
That is an argument for connecting Gmail before writing any new connector, not for
scraping.

## Sources with no connector

`manual` quick-adds and one-off imports write `source_item` rows without a credential
row behind them. They cite into the brief like anything else and no sync will ever
refresh them, so the Sources panel and `backglass status` name them explicitly
(`unmanaged_sources.sql`) rather than reading FROM `credential` and showing nothing.
An import that quietly looks like a live feed is the docs/11 §8 failure — a view that
looks complete and is not.

## Credentials

Stored in the `credential` table, not environment variables:

```
id, user_id, source, access_token, refresh_token,
expires_at, cursor, scopes, status, last_error, updated_at
```

Env vars hold only the client ID and secret per provider, which are app-level rather than
user-level.

Rationale: per-user OAuth is the entire difference between a script and a product, and
the table shape is identical either way. Doing it now costs one migration file.

## Setting up Gmail/Calendar/Drive OAuth

Gmail, Calendar, and Drive all authenticate through one Google Cloud OAuth client — you
need exactly one, shared across all three, not one per connector.

1. **Create a Google Cloud project.** [console.cloud.google.com](https://console.cloud.google.com/)
   → project picker → New Project. Any name; it's just a container for the API access
   below and is never seen by anyone but you.
2. **Enable the APIs you plan to use.** In the project, go to APIs & Services →
   Library, and enable each of: **Gmail API**, **Google Calendar API**,
   **Google Drive API**. Skip whichever you don't need — `backglass auth <label> --source
   <gmail|calendar|drive>` only needs the matching API enabled.
3. **Create an OAuth client.** APIs & Services → Credentials → Create Credentials →
   OAuth client ID. If prompted, configure the OAuth consent screen first: **External**
   user type is fine for personal use (you'll see an "unverified app" warning during
   consent — that's expected and safe to click through, since it's your own app talking
   to your own account), and you don't need to submit it for Google's review since only
   you (as a test user) will ever authorize it. For the client itself, choose **Desktop
   app** as the application type — this matches how `backglass auth` runs the consent
   flow (`InstalledAppFlow.run_local_server`, a short-lived local redirect, not a web
   callback URL).
4. **Load the downloaded JSON into `.env`:**
   ```bash
   uv run backglass google-client ~/Downloads/client_secret_*.json
   ```
   This checks the client type before writing anything. Google's Credentials page hands
   out **web** clients just as readily as Desktop ones and the filenames are identical;
   a web client has no localhost redirect, so `backglass auth` would fail deep inside the
   consent flow with an error about redirect URIs that names nothing you can act on. The
   secret goes from the downloaded file straight into `.env` and is never printed.

   By hand, if you prefer:
   ```
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   ```
5. **Add an account label per mailbox/calendar/drive you want to read** — e.g.
   `GMAIL_ACCOUNTS=personal`, `CALENDAR_ACCOUNTS=personal`, `DRIVE_ACCOUNTS=personal`.
   Labels are yours to choose; `credential.source` becomes `gmail:personal` etc., which
   is why two Gmail accounts need two different labels, comma-separated.
6. **Run the consent flow once per label:**
   ```bash
   uv run backglass auth personal --source gmail
   uv run backglass auth personal --source calendar
   uv run backglass auth personal --source drive
   ```
   Each opens a browser for Google's consent screen and stores the resulting tokens in
   the `credential` table — read-only scopes only (`gmail.readonly` etc.), since this
   system never sends or writes anywhere.
7. **Verify:** `uv run backglass doctor` reports "google oauth client configured" and one
   line per authorized account; `uv run backglass sync` should then pull real items on
   the next run.

## Health and failure

Every connector reports health on each run. `status` transitions to `failed` on auth
expiry or repeated errors, and stays there until a successful fetch.

A failed connector:

- does not block other connectors
- surfaces in the dashboard Sources panel with the error
- is named at the top of the next brief
- causes a non-zero exit code at the end of the run

Auth expiry is a visible product state, not a log line.
