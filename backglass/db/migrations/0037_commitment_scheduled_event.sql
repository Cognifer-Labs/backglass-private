-- The calendar event that *is* this obligation, so the planner stops budgeting it twice.
--
-- `tasks/booking-pipeline-2026-08-27.md` Increment D. Commitment 588 is the case: ninety
-- minutes, open, due Sep 3, and the work it names is a Dreamscape Learn pod session
-- reserved for Sep 2 at 6:00pm and already sitting in `source_item` as a calendar event
-- (`calendar:apple`, id 11161). `plan/capacity.py` subtracts the event from the day's
-- capacity, correctly. `plan/planner.py::candidates` then offers the commitment to
-- `select`, which spends ninety more minutes on it — so the Sep 2 plan proposes a pod
-- session on top of the pod session, and the day is charged three hours for ninety
-- minutes of VR.
--
-- The same leak the 2026-08-27 lesson names one register up: two phases with no channel
-- between them, where the first spends something the second cannot honour. Here the
-- phases are the calendar and the ledger, and the missing channel is this column.
--
-- Nullable, and NULL for every existing row, so the planner's behaviour is unchanged by
-- the migration alone. `plan/planner.py::candidates` skips a commitment that has one and
-- reports it — a linked obligation is not overflow and not "not yet", it is *on the
-- calendar*, which is a third thing again and the only one of the three that is good news.
--
-- REFERENCES rather than a bare integer: an event the owner retracts should not leave a
-- commitment pointing at a row that no longer means anything. Retraction does not delete
-- the source item (0030 keeps it and records the retraction beside it), so the reference
-- survives — which is what lets a surface say "the event this was riding on was
-- cancelled" instead of silently returning ninety minutes to the board.

ALTER TABLE commitment ADD COLUMN scheduled_source_item_id INTEGER
  REFERENCES source_item(id);

-- Read on every plan, for every open commitment. Partial, because the column is NULL for
-- essentially all of them and an index over three hundred NULLs earns nothing.
CREATE INDEX idx_commitment_scheduled ON commitment(scheduled_source_item_id)
  WHERE scheduled_source_item_id IS NOT NULL;
