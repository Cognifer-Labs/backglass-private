-- Touches the ledger cannot see, recorded by the person they happened to.
--
-- `follow_up_section` states the problem in its own docstring: a curated profile with no
-- interactions ever has no evidence to cite, so it appears on the People page but never
-- in the brief, because a claim with no source does not ship (CLAUDE.md rule 1). That
-- describes exactly one kind of relationship — the one that exists only in person. A
-- conversation at a dinner produces no email, no calendar block and no commitment, so
-- the person who most needs reminding about is the person the brief can never mention.
--
-- The fix is not to relax rule 1 for this case. It is to create the evidence. A touch
-- the owner records is the owner's own claim, and `actions.quick_add` already
-- establishes what that looks like: their words become a manual `source_item` first, and
-- the record cites it. So `source_item_id` is NOT NULL here for the same reason it is on
-- `commitment` — every reader of a touch gets the same provenance columns, and the
-- brief's `assert t.source_row is not None` keeps holding for people whose entire
-- history is three dinners.
--
-- `kind` is what happened, not how it was delivered: `met` in person, `sent` for
-- outbound the owner wrote, `call`, and `note` for anything else worth resetting the
-- clock. It exists so the People page can say "met" rather than "interaction", and so a
-- reach-out that was sent and never answered is distinguishable from a conversation
-- that happened.
--
-- The unique index is rule 3 in the only form that fits a hand-entered record: the same
-- touch logged twice in a day is one touch. Not an hour, not an exact timestamp — a day,
-- because "I met Felipe" typed twice at 9:02 and 17:40 is one meeting being re-recorded,
-- and a duplicate would quietly halve the measured gap.
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

-- How often this relationship is worth a touch, in days. NULL means the global pair in
-- settings, which is the right default for the hundred profiles extraction created and
-- the wrong one for the handful the owner actually chose to keep warm: a professor met
-- once a semester and a co-founder are not the same relationship on the same clock.
ALTER TABLE entity ADD COLUMN touch_every_days INTEGER;
