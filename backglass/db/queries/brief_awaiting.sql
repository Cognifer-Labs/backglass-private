-- docs/05 §5 Awaiting others: owed to you, with age.
--
-- docs/03 on `direction`: "owed_to_me is the one nobody tracks manually and the one that
-- justifies the build." Age is measured from when the promise was made — the source
-- item's occurred_at — not from when the row was written, because a backfill would
-- otherwise report a six-month-old promise as one day old.
--
-- Params: :user_id, :today (ISO date), :confidence_threshold
SELECT
  c.id, c.direction, c.what, c.due_at, c.confidence, c.source_item_id,
  e.canonical_name AS counterparty,
  s.source, s.external_id AS source_external_id, s.occurred_at AS source_occurred_at,
  s.title AS source_title,
  CAST(julianday(date(:today)) - julianday(date(s.occurred_at)) AS INTEGER) AS age_days
FROM commitment c
JOIN source_item s ON s.id = c.source_item_id
LEFT JOIN entity e ON e.id = c.counterparty_entity_id
WHERE c.user_id = :user_id
  AND c.status = 'open'
  AND c.direction = 'owed_to_me'
  AND c.confidence >= :confidence_threshold
ORDER BY age_days DESC, date(c.due_at) ASC;
