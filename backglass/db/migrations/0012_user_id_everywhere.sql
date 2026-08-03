-- `user_id` on every table, which docs/03 and CLAUDE.md both list as settled and which
-- eight tables had quietly never honoured: target, checkpoint, checklist_tick,
-- plan_block, roadmap_step, roadmap_cadence, purge_gate, model_batch_item.
--
-- Nothing was broken by their absence — each one FK-cascades to a parent that does carry
-- user_id, so a single owner's data was never at risk of mixing. What was broken is the
-- claim: specs/schema.sql says "user_id is on every table and is always 1. Do not remove
-- it", and an invariant that is documented but not true is worse than one that is merely
-- absent, because the next person reads the file instead of the schema. The second user
-- this column exists for is exactly the moment nobody wants to discover which tables
-- opted out.
--
-- DEFAULT 1 with no REFERENCES clause, matching model_batch (0011): SQLite requires a
-- NULL default on any column added with a foreign key, which cannot coexist with NOT
-- NULL — so the house pattern states the value and leaves the constraint to the parent's
-- cascade, exactly as the existing tables do.
--
-- schema_version is deliberately excluded: it records which migrations this file has
-- seen, which is a property of the database rather than of an owner.

ALTER TABLE target           ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE checkpoint       ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE checklist_tick   ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE plan_block       ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE roadmap_step     ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE roadmap_cadence  ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE purge_gate       ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE model_batch_item ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
