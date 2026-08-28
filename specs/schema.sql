-- Backglass schema. SQLite.
--
-- GENERATED from backglass/db/migrations/ — do not hand-edit. Change the schema by
-- adding a migration, then run:  uv run python -m tests.test_schema_reference
-- tests/test_schema_reference.py fails if this file and the migrations disagree.
--
-- Rationale for each table lives in the migration that introduced it, and in
-- docs/03-data-model.md and docs/04-daily-schedule-and-goals.md §3.
--
-- user_id is on every table except schema_version (which records what this database
-- has applied, not whose data it is) and is always 1. Do not remove it.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE schema_version (
  version    INTEGER PRIMARY KEY,
  filename   TEXT    NOT NULL,
  checksum   TEXT    NOT NULL,       -- sha256 of the migration file as applied
  applied_at TEXT    NOT NULL
);

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
  updated_at    TEXT    NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  UNIQUE (user_id, source)
);

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
  extraction_version TEXT, template_hash TEXT,
  UNIQUE (user_id, source, external_id)
);

CREATE INDEX idx_source_hash    ON source_item(user_id, content_hash);

CREATE INDEX idx_source_pending ON source_item(user_id, extraction_version)
  WHERE triage_verdict = 'keep';

CREATE TABLE entity (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  kind           TEXT    NOT NULL,         -- person|org|project
  canonical_name TEXT    NOT NULL,
  aliases_json   TEXT    NOT NULL DEFAULT '[]',
  notes          TEXT, role TEXT, org TEXT, tags_json TEXT NOT NULL DEFAULT '[]', profile_json TEXT, updated_at TEXT, touch_every_days INTEGER,
  UNIQUE (user_id, kind, canonical_name)
);

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
, total_count INTEGER, user_id INTEGER NOT NULL DEFAULT 1, every_days INTEGER);

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
, scheduled_source_item_id INTEGER
  REFERENCES source_item(id));

CREATE INDEX idx_commitment_open ON commitment(user_id, status, due_at)
  WHERE status = 'open';

CREATE INDEX idx_commitment_awaiting ON commitment(user_id, direction, status)
  WHERE direction = 'owed_to_me' AND status = 'open';

CREATE INDEX idx_commitment_review ON commitment(user_id, confidence)
  WHERE status = 'open';

CREATE INDEX idx_commitment_goal ON commitment(goal_id) WHERE goal_id IS NOT NULL;

CREATE TABLE checkpoint (
  id             INTEGER PRIMARY KEY,
  target_id      INTEGER NOT NULL REFERENCES target(id) ON DELETE CASCADE,
  occurred_at    TEXT    NOT NULL,
  source         TEXT    NOT NULL,         -- block|commitment|manual|extraction
  source_item_id INTEGER REFERENCES source_item(id),
  commitment_id  INTEGER REFERENCES commitment(id),
  note           TEXT,
  delta          INTEGER NOT NULL DEFAULT 1
, activity_id INTEGER REFERENCES activity(id), user_id INTEGER NOT NULL DEFAULT 1);

CREATE INDEX idx_checkpoint_target ON checkpoint(target_id, occurred_at);

CREATE TABLE checklist_item (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL DEFAULT 1,
  title        TEXT    NOT NULL,
  weekday_mask INTEGER NOT NULL DEFAULT 127, -- bit 0 = Monday
  active       INTEGER NOT NULL DEFAULT 1,
  sort_order   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE checklist_tick (
  id                INTEGER PRIMARY KEY,
  checklist_item_id INTEGER NOT NULL REFERENCES checklist_item(id) ON DELETE CASCADE,
  local_date        TEXT    NOT NULL,      -- YYYY-MM-DD in the active timezone
  ticked_at         TEXT    NOT NULL, user_id INTEGER NOT NULL DEFAULT 1,
  UNIQUE (checklist_item_id, local_date)
);

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
, inputs_fingerprint TEXT);

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
, user_id INTEGER NOT NULL DEFAULT 1);

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
, kind TEXT NOT NULL DEFAULT 'sync', degrade_reason TEXT);

CREATE TRIGGER source_item_immutable
BEFORE UPDATE ON source_item
FOR EACH ROW
WHEN OLD.user_id      IS NOT NEW.user_id
  OR OLD.source       IS NOT NEW.source
  OR OLD.external_id  IS NOT NEW.external_id
  OR OLD.fetched_at   IS NOT NEW.fetched_at
  OR OLD.occurred_at  IS NOT NEW.occurred_at
  OR OLD.author       IS NOT NEW.author
  OR OLD.title        IS NOT NEW.title
  OR OLD.body_text    IS NOT NEW.body_text
  OR OLD.raw_json     IS NOT NEW.raw_json
  OR OLD.content_hash IS NOT NEW.content_hash
BEGIN
  SELECT RAISE(
    ABORT,
    'source_item is immutable; only triage_verdict, triage_reason and extraction_version may change (docs/03)'
  );
END;

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
  done_at      TEXT, user_id INTEGER NOT NULL DEFAULT 1,
  UNIQUE (roadmap_id, step_key)
);

CREATE INDEX idx_roadmap_step ON roadmap_step(roadmap_id, sort_order);

CREATE TABLE roadmap_cadence (
  id          INTEGER PRIMARY KEY,
  roadmap_id  INTEGER NOT NULL REFERENCES roadmap(id) ON DELETE CASCADE,
  cadence_key TEXT    NOT NULL,
  target_id   INTEGER NOT NULL REFERENCES target(id), user_id INTEGER NOT NULL DEFAULT 1,
  UNIQUE (roadmap_id, cadence_key)
);

CREATE TABLE entity_merge (
  id                  INTEGER PRIMARY KEY,
  user_id             INTEGER NOT NULL DEFAULT 1,
  winner_id           INTEGER NOT NULL REFERENCES entity(id),
  loser_snapshot_json TEXT    NOT NULL,
  merged_at           TEXT    NOT NULL
);

CREATE TABLE purge_gate (
  id   INTEGER PRIMARY KEY CHECK (id = 1),
  open INTEGER NOT NULL DEFAULT 0
, user_id INTEGER NOT NULL DEFAULT 1);

CREATE TRIGGER source_item_no_delete
BEFORE DELETE ON source_item
WHEN (SELECT open FROM purge_gate WHERE id = 1) = 0
BEGIN
  SELECT RAISE(ABORT, 'source_item rows are kept forever (docs/03); only the boundary purge (docs/08 D6) may delete');
END;

CREATE TABLE activity (
  id                INTEGER PRIMARY KEY,
  user_id           INTEGER NOT NULL DEFAULT 1,
  title             TEXT    NOT NULL,
  org               TEXT,
  role              TEXT,
  category          TEXT    NOT NULL DEFAULT 'other',
  contact_entity_id INTEGER REFERENCES entity(id),
  started_on        TEXT,                        -- YYYY-MM-DD
  ended_on          TEXT,                        -- NULL while ongoing
  is_ongoing        INTEGER NOT NULL DEFAULT 1,
  most_meaningful   INTEGER NOT NULL DEFAULT 0,  -- AMCAS allows 3; surfaced, not enforced
  active            INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT    NOT NULL
);

CREATE INDEX idx_checkpoint_activity ON checkpoint(activity_id) WHERE activity_id IS NOT NULL;

CREATE TABLE fact (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  subject        TEXT    NOT NULL,          -- kebab lane: identity|housing|premed|...
  key            TEXT    NOT NULL,
  value          TEXT    NOT NULL,
  note           TEXT,                      -- evidence pointer, in words
  source         TEXT    NOT NULL,          -- manual|extraction|assistant
  source_item_id INTEGER REFERENCES source_item(id),
  status         TEXT    NOT NULL DEFAULT 'active', -- active|superseded|retracted
  superseded_by  INTEGER REFERENCES fact(id),
  created_at     TEXT    NOT NULL
, confidence REAL);

CREATE INDEX idx_fact_active ON fact(user_id, subject, key) WHERE status = 'active';

CREATE TABLE learned_noise (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  kind           TEXT    NOT NULL CHECK (kind IN ('address', 'domain')),
  value          TEXT    NOT NULL,          -- lowercased bare address or domain
  evidence_count INTEGER NOT NULL,          -- model-drop verdicts at promotion time
  first_seen     TEXT,                      -- occurred_at of the earliest evidence item
  last_seen      TEXT,
  promoted_at    TEXT    NOT NULL,
  promoted_by    TEXT    NOT NULL DEFAULT 'cli',  -- 'cli' | 'auto'
  enabled        INTEGER NOT NULL DEFAULT 1,
  UNIQUE (user_id, kind, value)
);

CREATE INDEX idx_source_template ON source_item(user_id, template_hash)
  WHERE template_hash IS NOT NULL;

CREATE TABLE model_batch (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL DEFAULT 1,
  batch_id     TEXT    NOT NULL UNIQUE,          -- msgbatch_...
  kind         TEXT    NOT NULL DEFAULT 'extract',
  model        TEXT    NOT NULL,                  -- concrete model id at submit time
  prompt_stamp TEXT    NOT NULL,                  -- extraction_version this batch targets
  status       TEXT    NOT NULL DEFAULT 'submitted',
               -- submitted | collected | expired | failed | canceled
  created_at   TEXT    NOT NULL,
  collected_at TEXT,
  spend_cents  INTEGER NOT NULL DEFAULT 0,
  error        TEXT
);

CREATE TABLE model_batch_item (
  id             INTEGER PRIMARY KEY,
  batch_id       TEXT    NOT NULL REFERENCES model_batch(batch_id),
  custom_id      TEXT    NOT NULL,                -- "si-<source_item_id>"
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  status         TEXT    NOT NULL DEFAULT 'pending', user_id INTEGER NOT NULL DEFAULT 1,
                 -- pending | succeeded | errored | expired
  UNIQUE (batch_id, custom_id)
);

CREATE INDEX idx_batch_item_source ON model_batch_item(source_item_id);

CREATE TABLE commitment_evidence (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  commitment_id  INTEGER NOT NULL REFERENCES commitment(id),
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  quote          TEXT,                                    -- verbatim; NULL = document only
  kind           TEXT    NOT NULL DEFAULT 'original',     -- original|restated|manual
  seen_at        TEXT    NOT NULL,
  UNIQUE (user_id, commitment_id, source_item_id)
);

CREATE INDEX idx_commitment_evidence ON commitment_evidence(user_id, commitment_id, id);

CREATE INDEX idx_evidence_by_source  ON commitment_evidence(user_id, source_item_id);

CREATE TABLE engagement (
  id               INTEGER PRIMARY KEY,
  user_id          INTEGER NOT NULL DEFAULT 1,
  kind             TEXT    NOT NULL,                      -- social|professional
  what             TEXT    NOT NULL,
  starts_at        TEXT,                                  -- NULL = agreed in principle, no time yet
  ends_at          TEXT,
  when_is_explicit INTEGER NOT NULL DEFAULT 0,
  location         TEXT,
  status           TEXT    NOT NULL DEFAULT 'proposed',   -- proposed|confirmed|declined|done|superseded
  confidence       REAL    NOT NULL,
  source_item_id   INTEGER NOT NULL REFERENCES source_item(id),
  superseded_by    INTEGER REFERENCES engagement(id),
  created_at       TEXT    NOT NULL,
  resolved_at      TEXT
);

CREATE INDEX idx_engagement_upcoming ON engagement(user_id, status, starts_at)
  WHERE status IN ('proposed', 'confirmed');

CREATE TABLE engagement_person (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  engagement_id INTEGER NOT NULL REFERENCES engagement(id) ON DELETE CASCADE,
  entity_id     INTEGER NOT NULL REFERENCES entity(id),
  UNIQUE (user_id, engagement_id, entity_id)
);

CREATE INDEX idx_engagement_person_by_entity
  ON engagement_person(user_id, entity_id, engagement_id);

CREATE TABLE engagement_evidence (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  engagement_id  INTEGER NOT NULL REFERENCES engagement(id),
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  quote          TEXT,                                    -- verbatim; NULL = document only
  kind           TEXT    NOT NULL DEFAULT 'original',     -- original|restated|manual
  seen_at        TEXT    NOT NULL,
  UNIQUE (user_id, engagement_id, source_item_id)
);

CREATE INDEX idx_engagement_evidence ON engagement_evidence(user_id, engagement_id, id);

CREATE INDEX idx_engagement_evidence_by_source
  ON engagement_evidence(user_id, source_item_id);

CREATE TABLE monitored_chat (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  source        TEXT    NOT NULL,          -- imessage|instagram|instagram:live
  key           TEXT    NOT NULL,          -- title for a group, handle for a one-to-one
  display_name  TEXT,
  kind          TEXT    NOT NULL DEFAULT 'group',  -- group|dm
  decision      TEXT,                      -- monitor|ignore|NULL = not yet decided
  participants  INTEGER,                   -- best-effort, for the page's "12 people"
  messages_seen INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT    NOT NULL,
  last_seen_at  TEXT    NOT NULL,
  decided_at    TEXT, rechecked_through INTEGER,
  UNIQUE (user_id, source, key)
);

CREATE INDEX idx_monitored_chat_undecided ON monitored_chat(user_id, decision, last_seen_at);

CREATE TABLE decision (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  title          TEXT    NOT NULL,          -- what was decided about, owner's words
  choice         TEXT    NOT NULL,          -- what was decided
  reasoning      TEXT,                      -- why, optional
  commitment_id  INTEGER REFERENCES commitment(id),
  status         TEXT    NOT NULL DEFAULT 'active', -- active|superseded|retracted
  superseded_by  INTEGER REFERENCES decision(id),
  decided_at     TEXT    NOT NULL,
  created_at     TEXT    NOT NULL
);

CREATE INDEX idx_decision_active ON decision(user_id, status) WHERE status = 'active';

CREATE TABLE commitment_distinct (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL DEFAULT 1,
  low_id     INTEGER NOT NULL REFERENCES commitment(id),
  high_id    INTEGER NOT NULL REFERENCES commitment(id),
  decided_at TEXT    NOT NULL,
  UNIQUE (user_id, low_id, high_id)
);

CREATE TABLE model_call (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL DEFAULT 1,
  run_id       INTEGER REFERENCES run(id),
  tier         TEXT    NOT NULL,          -- triage | triage_batch | extract
  model        TEXT    NOT NULL,
  prompt_chars INTEGER NOT NULL DEFAULT 0,
  duration_ms  INTEGER NOT NULL DEFAULT 0,
  cost_usd     REAL    NOT NULL DEFAULT 0,
  outcome      TEXT    NOT NULL,          -- ok | error | rate_limited | auth
  started_at   TEXT    NOT NULL
);

CREATE INDEX idx_model_call_run ON model_call (user_id, run_id);

CREATE INDEX idx_model_call_tier ON model_call (user_id, tier, started_at);

CREATE TABLE open_question (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  kind          TEXT    NOT NULL,   -- conflict|untitled|duplicate_entity|contradiction|priority
  subject_key   TEXT    NOT NULL,   -- unique within kind; how a detector finds its own row
  question      TEXT    NOT NULL,   -- the sentence the owner reads
  detail        TEXT,               -- the evidence, rendered; never the only place it lives
  options_json  TEXT    NOT NULL DEFAULT '[]',
  status        TEXT    NOT NULL DEFAULT 'open',  -- open|answered|dismissed
  answer_option TEXT,               -- the option chosen, when one was
  answer_text   TEXT,               -- the owner's own words, always allowed
  asked_at      TEXT    NOT NULL,
  answered_at   TEXT,
  UNIQUE (user_id, kind, subject_key)
);

CREATE INDEX idx_open_question_open ON open_question(user_id, status) WHERE status = 'open';

CREATE TABLE embedding (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL DEFAULT 1,
  -- source_item | commitment. Part of the identity, so one ref_id may be embedded once
  -- per kind without collision.
  kind       TEXT    NOT NULL,
  ref_id     INTEGER NOT NULL,
  model      TEXT    NOT NULL,
  dim        INTEGER NOT NULL,
  vector     BLOB    NOT NULL,
  text_hash  TEXT    NOT NULL,
  chars      INTEGER NOT NULL,
  created_at TEXT    NOT NULL,
  UNIQUE (user_id, kind, ref_id, model)
);

CREATE INDEX idx_embedding_lookup ON embedding(user_id, kind, model);

CREATE TABLE touchpoint (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  entity_id      INTEGER NOT NULL REFERENCES entity(id),
  kind           TEXT    NOT NULL,          -- met|sent|call|note
  occurred_at    TEXT    NOT NULL,          -- when it happened, in the owner's local time
  note           TEXT,                      -- their words; also the source item's body
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  created_at     TEXT    NOT NULL
);

CREATE UNIQUE INDEX idx_touchpoint_once
  ON touchpoint(user_id, entity_id, kind, substr(occurred_at, 1, 10));

CREATE INDEX idx_touchpoint_person ON touchpoint(user_id, entity_id, occurred_at);

CREATE TABLE commitment_recheck (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  commitment_id  INTEGER NOT NULL REFERENCES commitment(id),
  verdict        TEXT    NOT NULL,          -- done|dropped|open
  confidence     REAL    NOT NULL,
  -- The message that settles it. NOT NULL: see above.
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  quote          TEXT    NOT NULL,          -- verbatim, from that message
  reason         TEXT,
  status         TEXT    NOT NULL DEFAULT 'pending',  -- pending|applied|dismissed
  created_at     TEXT    NOT NULL,
  decided_at     TEXT
);

CREATE UNIQUE INDEX idx_recheck_once
  ON commitment_recheck(user_id, commitment_id, source_item_id);

CREATE INDEX idx_recheck_pending
  ON commitment_recheck(user_id, status, created_at);

CREATE TABLE notification (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  kind           TEXT    NOT NULL,  -- overdue-today|questions-waiting|plan-replaced|plan-drift
  subject_key    TEXT    NOT NULL,  -- dedup identity within (kind, local_date)
  local_date     TEXT    NOT NULL,  -- the owner's local day this belongs to
  title          TEXT    NOT NULL,
  body           TEXT    NOT NULL,
  delivered      TEXT    NOT NULL,  -- osascript|failed: <why>
  created_at     TEXT    NOT NULL,
  UNIQUE (user_id, kind, subject_key, local_date)
);

CREATE TABLE logic_check (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  commitment_id  INTEGER NOT NULL REFERENCES commitment(id),
  verdict        TEXT    NOT NULL,           -- keep|nonsense
  confidence     REAL    NOT NULL,
  fact_id        INTEGER REFERENCES fact(id),
  quote          TEXT,
  reason         TEXT,
  status         TEXT    NOT NULL DEFAULT 'pending', -- applied|pending|kept
  created_at     TEXT    NOT NULL,
  decided_at     TEXT,
  UNIQUE (user_id, commitment_id)
);

CREATE INDEX idx_logic_check_pending ON logic_check(user_id, status) WHERE status = 'pending';

CREATE TABLE source_item_retraction (
  source_item_id INTEGER PRIMARY KEY REFERENCES source_item(id),
  user_id        INTEGER NOT NULL DEFAULT 1,
  retracted_at   TEXT    NOT NULL,
  reason         TEXT    NOT NULL
);

CREATE INDEX idx_retraction_user ON source_item_retraction(user_id);

CREATE TABLE assignment (
  id               INTEGER PRIMARY KEY,
  user_id          INTEGER NOT NULL DEFAULT 1,
  source           TEXT    NOT NULL,              -- canvas:ics
  external_id      TEXT    NOT NULL,              -- assignment:7833000, the item's own id
  source_item_id   INTEGER REFERENCES source_item(id),
  course           TEXT    NOT NULL DEFAULT '',
  title            TEXT    NOT NULL,
  due_at           TEXT,
  url              TEXT    NOT NULL DEFAULT '',
  description      TEXT    NOT NULL DEFAULT '',
  -- What "changed" means for this row. Not the fetch time: a feed re-read that returns
  -- the same document must write nothing (rule 3), and a description edit must re-open
  -- the effort question that was answered from the old text.
  description_hash TEXT    NOT NULL,
  effort_minutes   INTEGER,
  effort_basis     TEXT,                          -- stated_video|stated_words|…|type:<k>
  effort_quote     TEXT,                          -- the words the number was read from
  sessions         INTEGER NOT NULL DEFAULT 1,
  analyzed_hash    TEXT,                          -- the description_hash it was read from
  first_seen_at    TEXT    NOT NULL,
  -- Last *changed*, not last seen. A feed re-read that returns the same document must
  -- write nothing (rule 3), and a column bumped on every fetch would make every sync a
  -- write for all 159 rows — so the honest name is the one that matches the behaviour.
  -- "Did the feed still list it today" is `retraction`'s question and it has its own
  -- table; this one answers "when did this assignment last move".
  last_changed_at  TEXT    NOT NULL, points_possible  REAL, submitted_at     TEXT, submission_state TEXT, score            REAL, enriched_at      TEXT, unlock_at TEXT, lock_at   TEXT,
  UNIQUE (user_id, source, external_id)
);

CREATE INDEX idx_assignment_due ON assignment(user_id, due_at);

CREATE INDEX idx_assignment_item ON assignment(source_item_id);

CREATE TABLE assignment_material (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  assignment_id INTEGER NOT NULL REFERENCES assignment(id) ON DELETE CASCADE,
  kind          TEXT    NOT NULL,                 -- reading|software|link|document|other
  name          TEXT    NOT NULL,
  detail        TEXT    NOT NULL DEFAULT '',
  quote         TEXT    NOT NULL DEFAULT '',
  basis         TEXT    NOT NULL DEFAULT 'deterministic',  -- deterministic|model
  created_at    TEXT    NOT NULL,
  UNIQUE (user_id, assignment_id, kind, name)
);

CREATE INDEX idx_assignment_material ON assignment_material(user_id, assignment_id);

CREATE TABLE claim_event (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  at            TEXT    NOT NULL,
  subject_table TEXT    NOT NULL,   -- 'commitment' | 'fact' | 'open_question' | ...
  subject_id    INTEGER NOT NULL,
  field         TEXT,               -- the column that changed, or NULL for a whole-row event
  old_value     TEXT,
  new_value     TEXT,
  cause         TEXT    NOT NULL    -- free text: 'fact_superseded', 'logic:reported-done', ...
);

CREATE INDEX idx_claim_event_subject ON claim_event(user_id, subject_table, subject_id, id);

CREATE INDEX idx_claim_event_recent ON claim_event(user_id, at);

CREATE TABLE claim_dependency (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  subject_table TEXT    NOT NULL,
  subject_id    INTEGER NOT NULL,
  dep_key       TEXT    NOT NULL,
  kind          TEXT    NOT NULL,             -- 'fact' | 'none'
  fact_id       INTEGER REFERENCES fact(id),
  quote         TEXT,
  reason        TEXT    NOT NULL,
  status        TEXT    NOT NULL DEFAULT 'active',  -- active|superseded
  superseded_by INTEGER REFERENCES claim_event(id),
  created_at    TEXT    NOT NULL,
  UNIQUE (user_id, subject_table, subject_id, dep_key)
);

CREATE INDEX idx_claim_dependency_fact
  ON claim_dependency(user_id, fact_id) WHERE status = 'active';

CREATE TABLE situation_doc (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL DEFAULT 1,
  body       TEXT    NOT NULL,
  body_hash  TEXT    NOT NULL,   -- sha256 of body; the gate that makes a write mean something
  created_at TEXT    NOT NULL
);

CREATE INDEX idx_situation_doc_recent ON situation_doc(user_id, id DESC);

CREATE TABLE engagement_distinct (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL DEFAULT 1,
  low_id     INTEGER NOT NULL REFERENCES engagement(id),
  high_id    INTEGER NOT NULL REFERENCES engagement(id),
  decided_at TEXT    NOT NULL,
  UNIQUE (user_id, low_id, high_id)
);

CREATE INDEX idx_commitment_scheduled ON commitment(scheduled_source_item_id)
  WHERE scheduled_source_item_id IS NOT NULL;
