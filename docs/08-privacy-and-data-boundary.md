# Privacy and the Data Boundary

Read this before writing the Gmail connector. It has legal weight, not just hygiene, and
retrofitting it after the ledger is populated means throwing the ledger away.

## The situation

The owner's inbox carries correspondence for public health program clients, including
state WIC programs. Routing that through a personal side project and a third-party model
API is a materially different risk category from syncing a personal calendar.

This is not a hypothetical. It is the specific reason this document exists and why the
decision belongs in phase 0.

## The decision, required before any ingestion

Pick one. There is no third option and no "decide later."

### Option A: hard exclusion (recommended for v1)

Client correspondence never enters the system.

- A denylist of client sender and recipient domains, evaluated **before** the item is
  written to `source_item`, not before extraction.
- Any message with a denylisted address in `From`, `To`, `Cc`, or `Bcc` is dropped
  entirely. Not stored, not hashed, not counted beyond a tally.
- The tally is surfaced so the exclusion is visible: "142 messages excluded by data
  boundary this week."
- Attachments and Drive files inherit the same rule by ownership and sharing.

Cost: the system does not help with the largest single source of the owner's
commitments. That is a real loss and it is the right trade for v1.

### Option B: full scope with matching controls

The whole system is treated as in-scope for whatever the client agreements require.
That means, at minimum, understanding and satisfying obligations around processing
location, subprocessors, retention, encryption at rest, access logging, and breach
notification, and confirming that the model provider's terms permit the data category.

Cost: this is a compliance project, not a weekend build. Do not drift into it by
accident.

## Implementation requirements if Option A

| ID | Requirement |
|----|-------------|
| D1 | The boundary check runs in the connector, before persistence. Not in extraction, not in a filter over stored rows. |
| D2 | Denylist is configuration, versioned in the repo, and reviewed when a client is added. |
| D3 | Match on domain and on explicit individual addresses, case-insensitive, including subdomains. |
| D4 | A message matching the denylist produces no `source_item` row and no model call. |
| D5 | Excluded counts are recorded per run and shown in the Sources panel, so a misconfigured boundary that excludes everything is immediately visible. |
| D6 | Adding a domain to the denylist triggers a purge of any previously stored items matching it, with a report of what was removed. |
| D7 | A test asserts that a message with a denylisted address in any recipient field produces zero rows. This test does not get skipped. |

D6 exists because the denylist will be incomplete on day one. There must be a clean way
to fix that discovery.

## General handling

Independent of which option is chosen:

- Tokens never appear in logs, in the SQLite file outside the `credential` table, or in
  brief output.
- `body_text` is never included in error reports or exception traces.
- The SQLite file lives in a directory excluded from any backup or sync tool that leaves
  the machine, unless that destination is itself in scope.
- The brief is sent to one address, configured, and never CC'd.
- No telemetry leaves the machine. There is one user and they can read the logs.

## What this system must never do

- Send email, reply, or modify anything in a source account.
- Store credentials for accounts the owner does not personally control.
- Include extracted content in any prompt sent to a provider other than the configured
  one.
- Retain excluded content in any form, including hashes computed over it.
