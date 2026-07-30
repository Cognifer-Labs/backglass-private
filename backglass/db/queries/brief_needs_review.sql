-- docs/05 §8 Needs review: low-confidence extractions, accept or reject.
--
-- CLAUDE.md rule 2. These are guesses, and design-system.md §4 renders them with a dashed
-- keyline and no fill: "in a system where color means certainty, a guess does not get an
-- ink." They appear in the brief as questions, never as claims.
--
-- Params: :user_id, :confidence_threshold
SELECT
  c.id, c.direction, c.what, c.due_at, c.confidence,
  e.canonical_name AS counterparty,
  s.source, s.external_id AS source_external_id, s.occurred_at AS source_occurred_at,
  s.title AS source_title
FROM commitment c
JOIN source_item s ON s.id = c.source_item_id
LEFT JOIN entity e ON e.id = c.counterparty_entity_id
WHERE c.user_id = :user_id
  AND c.status = 'open'
  AND c.confidence < :confidence_threshold
ORDER BY c.confidence DESC, date(c.due_at) ASC;
