-- Backglass schema. SQLite.
-- Rationale in docs/03-data-model.md and docs/04-daily-schedule-and-goals.md §3.
-- user_id is on every table and is always 1. Do not remove it.
--
-- This file is a frozen snapshot of specs/schema.sql as it stood when migration 0001
-- was applied, plus the schema_version table that docs/10 §Storage requires and
-- specs/schema.sql omits. Migrations are immutable once applied; specs/schema.sql
-- remains the readable source of truth for structure. If you change this file after
-- it has been applied anywhere, the runner will refuse to start.

-- ─────────────────────────────────────────────────────────── migration state

CREATE TABLE schema_version (
  version    INTEGER PRIMARY KEY,
  filename   TEXT    NOT NULL,
  checksum   TEXT    NOT NULL,       -- sha256 of the migration file as applied
  applied_at TEXT    NOT NULL
);

-- The PRAGMAs in specs/schema.sql are deliberately absent here. `journal_mode` cannot be
-- set inside a transaction, and `foreign_keys` is per-connection rather than stored with
-- the database, so neither belongs in a migration. db.connect() sets both on every
-- connection, which is the only place that can actually guarantee them.

-- ─────────────────────────────────────────────────────────── credentials

CREATE TABLE credential (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  source        TEXT    NOT NULL,          -- gmail|drive|notes|calendar|canvas
  access_token  TEXT,
  refresh_token TEXT,
  expires_at    TEXT,
  cursor        TEXT,                      -- opaque per-connector position
  scopes        TEXT,
  status        TEXT    NOT NULL DEFAULT 'ok',   -- ok|failed|revoked
  last_error    TEXT,
  updated_at    TEXT    NOT NULL,
  UNIQUE (user_id, source)
);

-- ─────────────────────────────────────────────────────────── raw capture

-- Immutable. Written once, never updated except extraction_version/triage_verdict.
CREATE TABLE source_item (
  id                 INTEGER PRIMARY KEY,
  user_id            INTEGER NOT NULL DEFAULT 1,
  source             TEXT    NOT NULL,
  external_id        TEXT    NOT NULL,
  fetched_at         TEXT    NOT NULL,
  occurred_at        TEXT    NOT NULL,     -- when it happened; relative dates resolve here
  author             TEXT,
  title              TEXT,
  body_text          TEXT,
  raw_json           TEXT,
  content_hash       TEXT    NOT NULL,
  triage_verdict     TEXT,                 -- keep|drop|unclassified
  triage_reason      TEXT,
  extraction_version TEXT,
  UNIQUE (user_id, source, external_id)
);

CREATE INDEX idx_source_hash    ON source_item(user_id, content_hash);
CREATE INDEX idx_source_pending ON source_item(user_id, extraction_version)
  WHERE triage_verdict = 'keep';

-- ─────────────────────────────────────────────────────────── entities

CREATE TABLE entity (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  kind           TEXT    NOT NULL,         -- person|org|project
  canonical_name TEXT    NOT NULL,
  aliases_json   TEXT    NOT NULL DEFAULT '[]',
  notes          TEXT,
  UNIQUE (user_id, kind, canonical_name)
);

-- ─────────────────────────────────────────────────────────── goals

CREATE TABLE goal (
  id                INTEGER PRIMARY KEY,
  user_id           INTEGER NOT NULL DEFAULT 1,
  title             TEXT    NOT NULL,
  horizon           TEXT    NOT NULL,      -- annual|quarterly
  target_date       TEXT,
  definition_of_done TEXT   NOT NULL,      -- a goal without one is a mood
  status            TEXT    NOT NULL DEFAULT 'active', -- active|done|dropped|paused
  created_at        TEXT    NOT NULL,
  closed_at         TEXT
);

CREATE TABLE target (
  id                     INTEGER PRIMARY KEY,
  goal_id                INTEGER NOT NULL REFERENCES goal(id) ON DELETE CASCADE,
  kind                   TEXT    NOT NULL, -- cadence|milestone|maintenance
  title                  TEXT    NOT NULL,
  weekly_count           INTEGER,          -- NULL for milestone
  estimated_minutes_each INTEGER,          -- feeds the weekly capacity check
  active                 INTEGER NOT NULL DEFAULT 1,
  created_at             TEXT    NOT NULL
);

-- ─────────────────────────────────────────────────────────── commitments

CREATE TABLE commitment (
  id                    INTEGER PRIMARY KEY,
  user_id               INTEGER NOT NULL DEFAULT 1,
  direction             TEXT    NOT NULL,  -- i_owe|owed_to_me
  counterparty_entity_id INTEGER REFERENCES entity(id),
  what                  TEXT    NOT NULL,
  due_at                TEXT,
  estimated_minutes     INTEGER,
  estimate_source       TEXT,              -- extracted|type_default|manual
  confidence            REAL    NOT NULL,
  status                TEXT    NOT NULL DEFAULT 'open', -- open|done|dropped|superseded
  goal_id               INTEGER REFERENCES goal(id),     -- at most one, deliberately
  source_item_id        INTEGER NOT NULL REFERENCES source_item(id),
  superseded_by         INTEGER REFERENCES commitment(id),
  rollover_count        INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT    NOT NULL,
  resolved_at           TEXT,
  resolution_note       TEXT
);

CREATE INDEX idx_commitment_open ON commitment(user_id, status, due_at)
  WHERE status = 'open';
CREATE INDEX idx_commitment_awaiting ON commitment(user_id, direction, status)
  WHERE direction = 'owed_to_me' AND status = 'open';
CREATE INDEX idx_commitment_review ON commitment(user_id, confidence)
  WHERE status = 'open';
CREATE INDEX idx_commitment_goal ON commitment(goal_id) WHERE goal_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────── checkpoints

CREATE TABLE checkpoint (
  id             INTEGER PRIMARY KEY,
  target_id      INTEGER NOT NULL REFERENCES target(id) ON DELETE CASCADE,
  occurred_at    TEXT    NOT NULL,
  source         TEXT    NOT NULL,         -- block|commitment|manual|extraction
  source_item_id INTEGER REFERENCES source_item(id),
  commitment_id  INTEGER REFERENCES commitment(id),
  note           TEXT,
  delta          INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_checkpoint_target ON checkpoint(target_id, occurred_at);

-- ─────────────────────────────────────────────────────────── checklist

CREATE TABLE checklist_item (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL DEFAULT 1,
  title        TEXT    NOT NULL,
  weekday_mask INTEGER NOT NULL DEFAULT 127, -- bit 0 = Monday
  active       INTEGER NOT NULL DEFAULT 1,
  sort_order   INTEGER NOT NULL DEFAULT 0
);
-- Seven-item cap is enforced in application code, not here, so the error message
-- can explain why. See docs/04-daily-schedule-and-goals.md C1.

CREATE TABLE checklist_tick (
  id                INTEGER PRIMARY KEY,
  checklist_item_id INTEGER NOT NULL REFERENCES checklist_item(id) ON DELETE CASCADE,
  local_date        TEXT    NOT NULL,      -- YYYY-MM-DD in the active timezone
  ticked_at         TEXT    NOT NULL,
  UNIQUE (checklist_item_id, local_date)
);

-- ─────────────────────────────────────────────────────────── day planning

CREATE TABLE day_plan (
  id               INTEGER PRIMARY KEY,
  user_id          INTEGER NOT NULL DEFAULT 1,
  local_date       TEXT    NOT NULL,
  tz               TEXT    NOT NULL,       -- IANA name active for this day
  capacity_minutes INTEGER NOT NULL,
  planned_minutes  INTEGER NOT NULL DEFAULT 0,
  overflow_count   INTEGER NOT NULL DEFAULT 0,
  generated_at     TEXT    NOT NULL,
  accepted_at      TEXT,
  status           TEXT    NOT NULL DEFAULT 'proposed' -- proposed|accepted|superseded
);

CREATE INDEX idx_dayplan_current ON day_plan(user_id, local_date, status);

CREATE TABLE plan_block (
  id             INTEGER PRIMARY KEY,
  day_plan_id    INTEGER NOT NULL REFERENCES day_plan(id) ON DELETE CASCADE,
  starts_at      TEXT    NOT NULL,
  ends_at        TEXT    NOT NULL,
  kind           TEXT    NOT NULL,         -- fixed|work|protected|small|buffer
  commitment_id  INTEGER REFERENCES commitment(id),
  goal_id        INTEGER REFERENCES goal(id),
  title          TEXT    NOT NULL,
  pinned         INTEGER NOT NULL DEFAULT 0,
  outcome        TEXT    NOT NULL DEFAULT 'pending', -- pending|done|rolled|dropped
  rollover_count INTEGER NOT NULL DEFAULT 0  -- denormalized on purpose, read on every render
);

CREATE INDEX idx_block_plan ON plan_block(day_plan_id, starts_at);

CREATE TABLE shutdown_note (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL DEFAULT 1,
  local_date TEXT    NOT NULL,
  learned    TEXT,
  blocked    TEXT,
  created_at TEXT    NOT NULL,
  UNIQUE (user_id, local_date)
);

-- ─────────────────────────────────────────────────────────── output

CREATE TABLE brief (
  id                 INTEGER PRIMARY KEY,
  user_id            INTEGER NOT NULL DEFAULT 1,
  generated_for_date TEXT    NOT NULL,
  kind               TEXT    NOT NULL DEFAULT 'daily', -- daily|monday|friday
  content_md         TEXT    NOT NULL,
  items_json         TEXT    NOT NULL,
  word_count         INTEGER NOT NULL,
  sent_at            TEXT,
  opened_at          TEXT,                 -- the metric that matters most
  feedback           TEXT,
  UNIQUE (user_id, generated_for_date, kind)
);

-- ─────────────────────────────────────────────────────────── run telemetry

CREATE TABLE run (
  id                INTEGER PRIMARY KEY,
  user_id           INTEGER NOT NULL DEFAULT 1,
  started_at        TEXT    NOT NULL,
  finished_at       TEXT,
  items_fetched     INTEGER NOT NULL DEFAULT 0,
  items_triaged_out INTEGER NOT NULL DEFAULT 0,
  items_excluded    INTEGER NOT NULL DEFAULT 0,  -- data boundary, see docs/08
  items_extracted   INTEGER NOT NULL DEFAULT 0,
  writes            INTEGER NOT NULL DEFAULT 0,  -- must be 0 on an idempotent re-run
  spend_cents       INTEGER NOT NULL DEFAULT 0,
  degraded          INTEGER NOT NULL DEFAULT 0,  -- 1 when spend cap forced triage-only
  errors_json       TEXT
);
