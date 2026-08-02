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
4. **Copy the client ID and secret into `.env`:**
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
