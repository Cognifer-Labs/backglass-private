-- Promises that answer themselves, and the citation that proves it.
--
-- 84 open commitments come from `imessage` across 16 monitored conversations. 79 of them
-- have later messages in the same chat and not one has ever closed from one. That is
-- structural rather than a bug: closure has exactly one path today, prompt v9's
-- `resolves`, which fires when a *new* message announces it completes an earlier promise
-- ("here's that deck I owed you"). Mail works that way. Friends do not — "bring dress
-- shoes" is answered by bringing dress shoes and the thread moves on — so a forward-only
-- signal can never reach an obligation that lives in chat, and the board fills with dead
-- favours. That is precisely the noise that makes an owner stop trusting a board.
--
-- So the read is backwards: given a promise and the conversation that happened after it,
-- is the promise still live?
--
-- `source_item_id` is NOT NULL because silence is not evidence. A closing verdict must
-- name the message that closed it and quote it verbatim; a verdict that cannot is
-- discarded rather than stored. "Nobody mentioned it again" is exactly the reasoning that
-- would close every real obligation the owner has been quietly failing to do, and this
-- column is what makes that reasoning impossible to record.
--
-- `status` carries the asymmetry `commitments.apply` already uses. Above the confidence
-- threshold a verdict is applied and lands `applied`; below it, it waits as `pending` on
-- the review fragment for one click. A wrong open row is visible and dismissible; a wrong
-- close is silent data loss, and tasks/lessons.md has four entries about paying for that.
--
-- The unique index is rule 3 in the form that fits a model pass: the same commitment
-- closed by the same message is one verdict however many times the pass runs. The
-- watermark below is the cheaper half — a chat with nothing new costs no call at all.
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

-- The highest `source_item.id` this chat has been checked through. No new messages, no
-- call, zero writes — rule 3's test, and the thing that keeps a 30-minute cadence from
-- re-reading sixteen conversations against a model every half hour.
ALTER TABLE monitored_chat ADD COLUMN rechecked_through INTEGER;
