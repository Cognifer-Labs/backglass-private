# Data Model

Full DDL in `specs/schema.sql`. This file explains the choices; the SQL is the source of
truth for structure.

## Core tables

```
source_item      immutable raw capture, kept forever
entity           people, orgs, projects
commitment       the heart of the system
brief            generated output, kept for the feedback loop
credential       per-source OAuth tokens
```

Schedule and goal tables (`goal`, `target`, `checkpoint`, `checklist_item`,
`checklist_tick`, `day_plan`, `plan_block`, `shutdown_note`) are specified in
`docs/04-daily-schedule-and-goals.md` §3.

## source_item

```
id, user_id, source, external_id, fetched_at, occurred_at,
author, title, body_text, raw_json, content_hash,
triage_verdict, extraction_version
```

`content_hash` is the change-detection primitive. Hash matches what is stored, item is
skipped entirely and no model is invoked. This is what keeps a 30-minute cadence cheap.

`occurred_at` is when the thing happened, not when it was fetched. Every relative date
resolves against this. Getting it wrong makes "by Friday" in an old email mean this
Friday, which is the most damaging bug this system can have.

Unique on `(source, external_id, user_id)`.

## commitment

```
id, user_id, direction, counterparty_entity_id, what,
due_at, estimated_minutes, confidence, status, goal_id,
source_item_id, created_at, resolved_at, resolution_note,
rollover_count
```

`direction` is `i_owe` or `owed_to_me`. The second is the one nobody tracks manually and
the one that justifies the build.

`confidence` is not decoration. Below threshold the record goes to the dashboard review
queue rather than the brief. A brief that confidently reports a commitment you never made
is worse than no brief.

`status` is `open | done | dropped | superseded`. Superseded matters: a later message
resolving an earlier commitment updates status rather than creating a second row.

`goal_id` is nullable and at most one. Multi-goal linkage sounds useful and makes
progress uninterpretable.

## entity

```
id, user_id, kind, canonical_name, aliases_json, notes
```

Resolution merges "Dave", "David R.", and `drodriguez@…` into one entity. Alias table
with manual override. Get this wrong and the "awaiting others" view fragments into
duplicates and stops being useful.

## Design rules

**`user_id` on every table, always 1.** Adding it later means a migration across every
query already written. This is the cheapest bet in the project.

**Credentials in a table, not env vars.** Per-user OAuth is the entire difference between
a script and a product, and the table shape is the same either way.

**Extraction prompts are versioned and stored,** not inline in code. Needed for A/B
comparison and for re-extracting history.

**The ledger is source-agnostic.** Connectors come and go. A commitment extracted from
Slack in 2027 should fit the same row shape as one from Gmail today. If a field only
makes sense for one source, it belongs in `raw_json`, not in a column.

## Indices that matter

```sql
CREATE INDEX idx_commitment_open ON commitment(user_id, status, due_at)
  WHERE status = 'open';
CREATE INDEX idx_commitment_awaiting ON commitment(user_id, direction, status)
  WHERE direction = 'owed_to_me' AND status = 'open';
CREATE INDEX idx_source_hash ON source_item(user_id, content_hash);
CREATE INDEX idx_checkpoint_target ON checkpoint(target_id, occurred_at);
```

Partial indices because the open set stays small while the closed set grows without
bound, and every read path in the product cares only about the open set.

## Retention

Nothing is deleted. `source_item` grows forever and that is fine; a decade of one
person's extracted email is well under a gigabyte of text.

Tombstone rather than delete for commitments so a resolved item cannot be resurrected by
a later re-extraction of the same source.
