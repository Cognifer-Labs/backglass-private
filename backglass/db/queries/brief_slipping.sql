-- docs/05 §4 Slipping: "due within 48 hours with no visible progress."
--
-- Overdue items are included rather than given their own section. An item three days late
-- is the most slipping thing in the ledger, and docs/05 has no overdue section for it to
-- land in — dropping it because it fell off the near end of a window would be the worst
-- possible reading of "due within 48 hours".
--
-- "No visible progress" is, at Phase 2, simply still-open: checkpoints and plan blocks
-- arrive in Phase 4, and this query gains a NOT EXISTS against them then.
--
-- Only commitments at or above the confidence threshold. CLAUDE.md rule 2: low-confidence
-- extractions go to the review queue, never into the brief as fact.
--
-- Params: :user_id, :horizon (ISO date), :confidence_threshold
SELECT
  c.id, c.direction, c.what, c.due_at, c.confidence, c.rollover_count,
  e.canonical_name AS counterparty,
  s.source, s.external_id AS source_external_id, s.occurred_at AS source_occurred_at,
  s.title AS source_title
FROM commitment c
JOIN source_item s ON s.id = c.source_item_id
LEFT JOIN entity e ON e.id = c.counterparty_entity_id
WHERE c.user_id = :user_id
  AND c.status = 'open'
  AND c.direction = 'i_owe'
  AND c.due_at IS NOT NULL
  AND date(c.due_at) <= date(:horizon)
  AND c.confidence >= :confidence_threshold
ORDER BY date(c.due_at) ASC, c.confidence DESC;
