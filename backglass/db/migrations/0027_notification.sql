-- Notifications get a ledger (tasks/todo.md increment 5).
--
-- A notification is an outbound claim about the owner's day, so it follows the same
-- two rules every generated claim does: provenance (rule 1 — the row IS the record of
-- what was said, when, and why), and idempotency (rule 3 — the UNIQUE key makes
-- "notify once per (kind, subject, local day)" a property of the schema rather than
-- of whichever caller remembered to check). Delivery is best-effort osascript; a
-- failed banner still leaves its row, because "what did the system tell the owner"
-- must be answerable from the ledger, not from Notification Center's memory.
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
