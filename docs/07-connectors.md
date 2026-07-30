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

## Health and failure

Every connector reports health on each run. `status` transitions to `failed` on auth
expiry or repeated errors, and stays there until a successful fetch.

A failed connector:

- does not block other connectors
- surfaces in the dashboard Sources panel with the error
- is named at the top of the next brief
- causes a non-zero exit code at the end of the run

Auth expiry is a visible product state, not a log line.
