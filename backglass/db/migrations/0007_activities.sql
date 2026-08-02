-- Activity registry. A discrete extracurricular activity —
-- organization, role, supervisor, date range — that hour checkpoints can attach to,
-- so the AMCAS Work & Activities section can later be assembled from evidence.
-- category maps to the medical preset's total keys (shadowing/clinical/volunteering/
-- research/leadership) or 'other'; it is a plain key, not an FK, so activities survive
-- roadmap re-instantiation. Per-activity hours are SUM(delta) on read (G3/G10) —
-- no cached counter exists to go stale.
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

-- Hours logged against a total target may now also name the activity they belong to.
-- Nullable and additive: every existing checkpoint and every non-activity log keeps
-- working unchanged.
ALTER TABLE checkpoint ADD COLUMN activity_id INTEGER REFERENCES activity(id);

CREATE INDEX idx_checkpoint_activity ON checkpoint(activity_id) WHERE activity_id IS NOT NULL;
