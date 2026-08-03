-- docs/05 §7 Rollover: items rolling over a third time, with the drop-or-do question.
--
-- `rollover_count` is only incremented by the day planner, which is Phase 4. Until then
-- this returns nothing and the section is omitted by B3, which is the correct behaviour
-- rather than a stub: there genuinely are no rollovers before there is a planner.
--
-- Params: :user_id, :min_rollovers
SELECT
  c.id, c.direction, c.what, c.due_at, c.confidence, c.rollover_count,
  c.source_item_id,
  e.canonical_name AS counterparty,
  s.source, s.external_id AS source_external_id, s.occurred_at AS source_occurred_at,
  s.title AS source_title
FROM commitment c
JOIN source_item s ON s.id = c.source_item_id
LEFT JOIN entity e ON e.id = c.counterparty_entity_id
WHERE c.user_id = :user_id
  AND c.status = 'open'
  AND c.rollover_count >= :min_rollovers
ORDER BY c.rollover_count DESC, date(c.due_at) ASC;
