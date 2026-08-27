-- Source items the upstream store no longer has, recorded without touching the item.
--
-- The owner's class schedule changed on ~2026-08-10: LIA 101 dropped, a BIO 181 lecture
-- moved Thu→Wed, and a BIO lab, a CHM lab and a CHM recitation disappeared. Calendar.app
-- was right about all of it. Backglass was not: it kept both timetables, and the day
-- planner went on subtracting five class slots that no longer meet from the owner's
-- capacity. `uv run backglass doctor` could not see it either — every row was healthy,
-- there were simply too many of them.
--
-- Why this is a table rather than a column: `source_item` is immutable, enforced by the
-- trigger in migration 0002, and docs/03 says raw items are kept forever. Both are right
-- and neither is negotiable. "This event is gone upstream" is not a correction to what
-- was collected — the row was true when it was written — so it is recorded *beside* the
-- item as a second fact with its own timestamp, and the original stays readable forever.
--
-- Why not deletion through the 0005 gate, the way `imessage.prune` does it: that gate
-- exists for items the boundary says must never have been stored (docs/08, legal weight).
-- A cancelled class is ordinary history. Next year's reader asking "what did my week look
-- like in August" deserves to see that CHM 113 met on Wednesdays until the 10th.
--
-- `reason` names the mechanism, not the meaning: the reconciler that wrote the row and
-- the window it was sure about. It is what makes a wrong retraction diagnosable instead
-- of mysterious.
--
-- One row per item, ever. Re-retracting an item that is already retracted is a no-op, so
-- the reconciler stays idempotent (rule 3) with no read-before-write.
CREATE TABLE source_item_retraction (
  source_item_id INTEGER PRIMARY KEY REFERENCES source_item(id),
  user_id        INTEGER NOT NULL DEFAULT 1,
  retracted_at   TEXT    NOT NULL,
  reason         TEXT    NOT NULL
);

CREATE INDEX idx_retraction_user ON source_item_retraction(user_id);
