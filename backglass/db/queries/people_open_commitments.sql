-- Open commitments with one person, both directions. Same provenance columns the
-- board query exposes, so templates can reuse the source_link macro.
--
-- Params: :user_id, :entity_id
SELECT
  c.id, c.direction, c.what, c.due_at, c.confidence, c.rollover_count,
  s.source, s.external_id AS source_external_id,
  s.occurred_at AS source_occurred_at, s.title AS source_title
FROM commitment c
JOIN source_item s ON s.id = c.source_item_id
WHERE c.user_id = :user_id
  AND c.counterparty_entity_id = :entity_id
  AND c.status = 'open'
ORDER BY
  CASE WHEN c.direction = 'i_owe' THEN 0 ELSE 1 END,
  c.due_at IS NULL,
  date(c.due_at) ASC;
