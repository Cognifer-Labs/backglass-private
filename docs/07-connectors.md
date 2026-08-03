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
safer half of it easy. `INSTAGRAM_CHATS` still gates everything — see below.

**Allowlist, not inbox.** `INSTAGRAM_CHATS` names the only group-chat titles and people
either lane reads — the Slack rule again: a personal tool reads the handful of threads
the owner names. Everything else is counted (`allowlist` rule) and never stored.

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

## Local and later-phase sources

The sections above are the network sources the first phases were built around. These
were added after, share the same `Connector` protocol, and are gated the same way — one
env key, no key means the connector is never constructed. They are listed here because
the protocol is the contract: a source that is not in this file is a source nobody
audits.

| source name | gate | cursor | boundary | how it fails |
|---|---|---|---|---|
| `imessage` | `IMESSAGE_DB_PATH` + `IMESSAGE_CHATS` | max `message.ROWID` | yes — handles are addresses | without Full Disk Access the file still stats — only the *open* is refused, with `unable to open database file` rather than anything that says "denied" |
| `apple-notes` | `APPLE_NOTES=1` | modification-date watermark | yes — note bodies carry addresses | Automation permission denied, reported by `health()` |
| `calendar:apple` | `APPLE_CALENDAR=1` | none — a bounded window, re-read each run | yes — titles and locations can carry addresses | Automation permission denied, reported by `health()` |
| `reminders` | `APPLE_REMINDERS=1` | fetch-window watermark (no mtime exists) | yes | same Automation prompt as Notes |
| `files` | `INBOX_FOLDER_PATH` | mtime watermark | yes | unsupported file types are counted and reported, never silently skipped |
| `github` | `GITHUB_TOKEN` | two watermarks in one string: search time + notifications `Last-Modified` | yes | 401 on a revoked token; the search quota is per-minute, so requests stay serialized |
| `anki` | `ANKI_DB_PATH` | revlog row range | none — tallies carry no addresses and no card text | Anki holding the write lock past the busy timeout degrades the source for one cycle |
| `avorio` | `AVORIO_DB_PATH` | `MAX(reviews.reviewed_at)` | none, same reason | schema drift; `health()` verifies every required table and column, not just the file |

Two rules those local stores exist to teach:

- **Open a live SQLite store `mode=ro` with a busy timeout, never `immutable=1`.** All
  three of these stores are WAL; `immutable` makes SQLite skip the `-wal` file, so a read
  is either silently stale or fails with "no such table" while the app is open.
- **An `external_id` must name content that cannot be recomputed differently.** Key a
  batch item to the exact immutable row range it summarizes. A high-watermark id
  (`reviews:<day>:<max-id>`) re-emits the same id with different content after a rescan,
  which the 0002 immutability trigger turns into a failed-looking sync.

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
`messages/inbox`. Set `INSTAGRAM_CHATS` to the threads worth reading — the connector is
allowlist-only by design, because a DM archive is the least filtered thing the owner owns.
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
