-- Phase 6: career layer. Rationale in tasks/todo.md §Phase 6 and the approved plan.
-- Bare statements: migrate() wraps this file in BEGIN/COMMIT itself.

-- ── entity enrichment ───────────────────────────────────────────────────
-- Structured columns only for what search filters on; everything else keeps the
-- docs/03 raw_json philosophy and goes in profile_json.
ALTER TABLE entity ADD COLUMN role TEXT;
ALTER TABLE entity ADD COLUMN org TEXT;
ALTER TABLE entity ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE entity ADD COLUMN profile_json TEXT;
ALTER TABLE entity ADD COLUMN updated_at TEXT;

-- ── roadmaps ────────────────────────────────────────────────────────────
-- One roadmap owns one goal row; steps and cadences own target rows. The goal
-- engine (progress, staleness, risk, capacity) reads roadmaps with zero changes.
CREATE TABLE roadmap (
  id                       INTEGER PRIMARY KEY,
  user_id                  INTEGER NOT NULL DEFAULT 1,
  path_id                  TEXT    NOT NULL,
  path_version             TEXT    NOT NULL,
  title                    TEXT    NOT NULL,
  goal_id                  INTEGER NOT NULL REFERENCES goal(id),
  status                   TEXT    NOT NULL DEFAULT 'active',  -- active|done|dropped
  personalized             INTEGER NOT NULL DEFAULT 0,
  interview_source_item_id INTEGER REFERENCES source_item(id),
  created_at               TEXT    NOT NULL,
  closed_at                TEXT
);

CREATE TABLE roadmap_step (
  id           INTEGER PRIMARY KEY,
  roadmap_id   INTEGER NOT NULL REFERENCES roadmap(id) ON DELETE CASCADE,
  step_key     TEXT    NOT NULL,           -- preset key; 'interview:<slug>'/'manual:<slug>' when added later
  title        TEXT    NOT NULL,
  detail       TEXT,
  sort_order   INTEGER NOT NULL,
  planned_date TEXT,
  status       TEXT    NOT NULL DEFAULT 'pending',  -- pending|active|done|skipped
  target_id    INTEGER REFERENCES target(id),
  origin       TEXT    NOT NULL DEFAULT 'preset',   -- preset|interview|manual
  done_at      TEXT,
  UNIQUE (roadmap_id, step_key)
);
CREATE INDEX idx_roadmap_step ON roadmap_step(roadmap_id, sort_order);

CREATE TABLE roadmap_cadence (
  id          INTEGER PRIMARY KEY,
  roadmap_id  INTEGER NOT NULL REFERENCES roadmap(id) ON DELETE CASCADE,
  cadence_key TEXT    NOT NULL,
  target_id   INTEGER NOT NULL REFERENCES target(id),
  UNIQUE (roadmap_id, cadence_key)
);

-- ── entity merge audit ──────────────────────────────────────────────────
-- The loser row is deleted; its full snapshot is not. Undo is manual but possible.
CREATE TABLE entity_merge (
  id                  INTEGER PRIMARY KEY,
  user_id             INTEGER NOT NULL DEFAULT 1,
  winner_id           INTEGER NOT NULL REFERENCES entity(id),
  loser_snapshot_json TEXT    NOT NULL,
  merged_at           TEXT    NOT NULL
);

-- ── run telemetry gains a kind ──────────────────────────────────────────
-- The roadmap interview spends model money outside a sync; the monthly cap must
-- see it. sync|interview.
ALTER TABLE run ADD COLUMN kind TEXT NOT NULL DEFAULT 'sync';
