-- docs/05 §8 Needs review: low-confidence extractions, accept or reject.
--
-- CLAUDE.md rule 2. These are guesses, and design-system.md §4 renders them with a dashed
-- keyline and no fill: "in a system where color means certainty, a guess does not get an
-- ink." They appear in the brief as questions, never as claims.
--
-- Ordered by what a decision would change, not by how sure the model is. Confidence
-- first put the model's best guesses at the top, which is a fact about the extraction
-- and not about the owner's day: of 158 rows in the real queue, 47 were `owed_to_me` —
-- work `planner.candidates` never schedules, so accepting one cannot alter any plan
-- ever — and 77 more carried no date and would land in the overflow tail. Thirty-four
-- bore on the coming week. A queue that asks those thirty-four last is a queue nobody
-- reaches the end of, and 158 unanswered questions is how it got to 158.
--
-- This orders the questions; it does not touch the answers. docs/11 §4's equal-weight
-- Accept and Reject stay exactly as they are — the rubber-stamp risk is in nudging
-- which button gets pressed, not in which card is read first.
--
-- Two boundaries. A date on or before Sunday is a question that reaches the coming
-- week; a date far enough past is one that has stopped reaching anything. The board
-- already draws that second line — "long-overdue rows fold into Stale rather than
-- crowding the lane a person scans" — and the queue uses the same `stale_after_days`,
-- because a guess about a deadline that passed in January is not made pressing by
-- having passed. Without it the top band held 178 of 303 rows, which is the original
-- problem with a different sort order.
--
-- Params: :user_id, :confidence_threshold, :week_end, :stale_floor
SELECT
  c.id, c.direction, c.what, c.due_at, c.confidence, c.source_item_id,
  CASE
    -- docs/04 §1.5: "an `owed_to_me` commitment is someone else's work; scheduling time
    -- for it would be scheduling time to wait." Never a candidate, so never plan-moving.
    WHEN c.direction <> 'i_owe'                     THEN 3
    WHEN c.due_at IS NULL                           THEN 2
    WHEN date(c.due_at) < date(:stale_floor)        THEN 2
    WHEN date(c.due_at) <= date(:week_end)          THEN 0
    ELSE 1
  END AS plan_impact,
  e.canonical_name AS counterparty,
  s.source, s.external_id AS source_external_id, s.occurred_at AS source_occurred_at,
  s.title AS source_title,
  -- A guess is exactly the case where the subject line is not enough: accepting or
  -- rejecting one means reading the sentence it came from (docs/11 §4 step 2).
  (SELECT ce.quote FROM commitment_evidence ce
    WHERE ce.commitment_id = c.id AND ce.quote IS NOT NULL
    ORDER BY ce.id LIMIT 1) AS evidence_quote,
  (SELECT COUNT(*) FROM commitment_evidence ce
    WHERE ce.commitment_id = c.id) AS mention_count
FROM commitment c
JOIN source_item s ON s.id = c.source_item_id
LEFT JOIN entity e ON e.id = c.counterparty_entity_id
WHERE c.user_id = :user_id
  AND c.status = 'open'
  AND c.confidence < :confidence_threshold
-- Impact first, then the old ordering intact inside each band: an overdue guess the
-- model is sure about still leads the overdue guesses.
ORDER BY plan_impact ASC, c.confidence DESC, date(c.due_at) ASC;
