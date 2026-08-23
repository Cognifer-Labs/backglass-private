-- Every loop pass leaves a row (tasks/todo.md goal 3, increment 2).
--
-- The five passes after the pipeline used to fail silently: `except Exception: pass`
-- under a rule 5 comment, which is half the rule. Log, surface, continue, exit non-zero
-- is the rule; continue was the only part implemented. A detector throwing for a week
-- and a detector with nothing to find produced the same output — nothing — so the loop
-- was unattended rather than autonomous.
--
-- This is the record that tells them apart. One row per pass per loop run, including the
-- quiet ones: "ran and found nothing" is the fact that proves the pass is alive, and a
-- table that only holds failures cannot answer "when did this last run at all", which is
-- the question `state` and `heartbeat` actually ask.
--
-- `run_id` is nullable and unenforced by design. A loop fired from the app-open trigger
-- belongs to no sync run, and the CLI's loop is recorded after `record_run` returns —
-- so a crash between them must leave the pass rows readable rather than orphaned into a
-- constraint failure. Same stance `model_call` takes for the same reason.
CREATE TABLE loop_pass (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL DEFAULT 1,
  run_id      INTEGER,           -- the sync run this rode with, when there was one
  name        TEXT    NOT NULL,  -- catchup|replan|logic|questions|duplicates|notify
  trigger     TEXT    NOT NULL,  -- clock|data|always, as the pass declared it
  status      TEXT    NOT NULL,  -- ok|failed|skipped
  local_date  TEXT    NOT NULL,  -- the owner's local day this ran on
  started_at  TEXT    NOT NULL,
  finished_at TEXT    NOT NULL,
  detail      TEXT               -- the lines it reported, or the exception that stopped it
);

-- Two clocks on one row, and they answer different questions, so both are stored rather
-- than one being derived from the other. `started_at`/`finished_at` are wall-clock UTC and
-- answer "how long did this take" — a frozen clock would make every pass look
-- instantaneous. `local_date` is the owner's day and is what a once-a-day gate must read:
-- the owner moves between UTC-7 and UTC+05:30, so 20:00 Phoenix is already tomorrow in
-- UTC, and a gate comparing UTC timestamps would reopen at dinner. `notification` carries
-- `local_date` for the same reason and it is the same reason.
--
-- "When did this pass last succeed?" is the one question every reader asks, and it is
-- asked per name. Without this it is a scan of a table that grows by five rows every
-- thirty minutes — 288 a day, and the dashboard asks on every page load.
CREATE INDEX loop_pass_by_name ON loop_pass (user_id, name, id DESC);
