-- Open commitments in one direction against one counterparty, for the dedup pass in
-- extract-commitments.md §Post-processing step 5.
--
-- Scoped to (direction, counterparty) so the fuzzy comparison only ever runs over a
-- handful of rows. A thread restating the same promise must not produce five rows.
--
-- `dropped` is included alongside `open`, and that is load-bearing rather than tidy.
-- docs/11 §4: "Reject tombstones it so re-extraction does not resurrect it." A rejected
-- extraction is the owner saying "this was never a commitment"; re-running extraction
-- against a better prompt must not overrule that judgement and put it back on the board.
--
-- `done` and `superseded` are deliberately NOT included. A commitment that was made,
-- kept, and then made again is a new commitment — suppressing the second one because it
-- reads like the first is how a recurring promise silently disappears.
--
-- Params: :user_id, :direction, :counterparty_entity_id
SELECT id, what, due_at, confidence, source_item_id, status
FROM commitment
WHERE user_id = :user_id
  AND status IN ('open', 'dropped')
  AND direction = :direction
  AND counterparty_entity_id IS :counterparty_entity_id;
