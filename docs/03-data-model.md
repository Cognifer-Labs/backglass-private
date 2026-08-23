# Data Model

Full DDL in `specs/schema.sql`. This file explains the choices; the SQL is the source of
truth for structure.

## Core tables

```
source_item      immutable raw capture, kept forever
entity           people, orgs, projects
commitment       the heart of the system
engagement       a plan to be somewhere with someone
brief            generated output, kept for the feedback loop
credential       per-source OAuth tokens
```

Schedule and goal tables (`goal`, `target`, `checkpoint`, `checklist_item`,
`checklist_tick`, `day_plan`, `plan_block`, `shutdown_note`) are specified in
`docs/04-daily-schedule-and-goals.md` §3.

Coursework tables (`assignment`, `assignment_material`, migration 0031, applied — schema
is at 31) sit beside the ledger rather than inside it: the obligation still lives in
`commitment` and the evidence still lives in `source_item`, and an assignment row exists
because a Canvas assignment is a live upstream record whose due date and instructions move
after the immutable item that first reported it was written. The migration's own header
carries the reasoning and is the file to read; it is not repeated here so the two cannot
drift.

There is no course table. `backglass/courses.py` — the reader behind the Classes page —
joins three vocabularies for one course (Canvas's `2026FallC-T-CHM113-60105`, the
calendar's `CHM 113 (Lab)`, the drop folder's `CHM113-Lab`) on the registrar's course
code, at read time, over rows that already exist. A course is not a record the system
owns; it is a view over meetings, coursework, commitments and documents.

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

## engagement

```
id, user_id, kind, what, starts_at, ends_at, when_is_explicit, location,
status, confidence, source_item_id, superseded_by, created_at, resolved_at

engagement_person    (engagement_id, entity_id)  — many people per plan
engagement_evidence  (engagement_id, source_item_id, quote, kind, seen_at)
```

A commitment answers "what do I still owe, and when is it late". An engagement answers
"who am I seeing, when, and have I answered them yet". Dinner on Friday, a conference, an
interview: nobody owes anyone an artifact, so none of them is a commitment, and before
this table they had nowhere to live. Migration `0014_engagements.sql` carries the full
reasoning for keeping them apart rather than adding a flag to `commitment`.

`kind` is `social|professional`. `status` is a lifecycle — `proposed → confirmed → done`,
or `→ declined` — and it only ever moves forward, so a later "still on for Friday?" cannot
unconfirm a plan. Only `confirmed` earns a block on the day plan; `proposed` is what the
owner still owes a reply to, and it is what the brief asks about.

`status` is also what keeps a cancellation cancelled. A `declined` row stays visible to
the dedup pass — the same reasoning that keeps `dropped` commitments in
`open_commitments_for_dedup` — because re-extraction re-reads the message that proposed
the plan after the one that called it off, and a dedup pass that cannot see the decision
files a fresh proposal for a dinner nobody is having.

Anything that names an entity has to be repointed by `people/merge.py` before the loser
row is deleted; `engagement_person`'s foreign key is NOT NULL, so forgetting it turns
merging two people into a rolled-back 500. `tests/test_people.py` derives that list from
`PRAGMA foreign_key_list` rather than hard-coding it.

`starts_at` is nullable, and that is the point: "we should get dinner sometime" is a real
plan with a real person and no time, and it is the one most likely to decay unnoticed.
It stores the time **as the message stated it** — the resolver never converts timezones —
so the column mixes bare dates, naive datetimes and offset-bearing ones. Compare and sort
it on `substr(starts_at, 1, 10)`, never through `date()`/`datetime()`, which normalise to
UTC first and walk an evening plan into the following day.

## entity

```
id, user_id, kind, canonical_name, aliases_json, role, org, tags_json, notes,
profile_json, updated_at
```

An entity's profile is read at query time from the evidence — channels, first and last
contact, how many mentions, whether the two of them meet socially or professionally —
rather than cached on the row. `backglass/people/profiles.py::derived` is that read. A
cached summary can be wrong in a way the underlying rows are not, and being checkable is
the whole product.

Resolution merges "Dave", "David R.", and `drodriguez@…` into one entity. Alias table
with manual override. Get this wrong and the "awaiting others" view fragments into
duplicates and stops being useful.

`touch_every_days` is how often this relationship is worth a touch, in days. NULL means
the global pair in settings — the right default for the hundred profiles extraction
created, and the wrong one for the handful the owner keeps warm on purpose.

## touchpoint

```
id, user_id, entity_id, kind, occurred_at, note, source_item_id, created_at
```

A touch the ledger cannot see: a dinner, a call, a message the owner sent. `kind` is
`met | sent | call | note` — what happened, not how it travelled.

It exists because the follow-up nudge could not otherwise mention the relationships that
need it most. A conversation in person produces no email, no calendar block and no
commitment, and a claim with no source does not ship (rule 1), so the person the owner
should be reminded about was the one the brief could never name. The answer is not to
relax the rule but to create the evidence: `source_item_id` is NOT NULL, and the owner's
own words become a manual source item first, exactly as `actions.quick_add` does for a
hand-entered commitment.

Unique on `(user_id, entity_id, kind, date(occurred_at))` — the same touch logged twice
in a day is one touch (rule 3). `people_cold.sql` takes the newest of commitment
evidence and touchpoints, so both kinds of history feed one clock.

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
