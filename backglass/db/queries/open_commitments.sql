-- The Phase 1 acceptance query (docs/09): the owner's open commitments.
--
-- Every row carries its provenance. CLAUDE.md rule 1 is "every generated claim links to
-- its source", and the cheapest way to keep that true is to make it impossible to read a
-- commitment out of this system without also reading where it came from.
--
-- Params: :user_id, :confidence_threshold
SELECT
  c.id,
  c.direction,
  c.what,
  c.due_at,
  c.confidence,
  c.estimated_minutes,
  c.estimate_source,
  c.rollover_count,
  c.created_at,
  e.canonical_name          AS counterparty,
  c.source_item_id,
  s.source                  AS source,
  s.external_id             AS source_external_id,
  s.occurred_at             AS source_occurred_at,
  s.title                   AS source_title,
  -- The sentence the claim rests on, and how many documents have said it. Both come
  -- from commitment_evidence (migration 0013); the quote is the first one recorded,
  -- because that is the one the owner has already been shown.
  (SELECT ce.quote FROM commitment_evidence ce
    WHERE ce.commitment_id = c.id AND ce.quote IS NOT NULL
    ORDER BY ce.id LIMIT 1)                          AS evidence_quote,
  (SELECT COUNT(*) FROM commitment_evidence ce
    WHERE ce.commitment_id = c.id)                   AS mention_count,
  CASE WHEN c.confidence < :confidence_threshold THEN 1 ELSE 0 END AS needs_review
FROM commitment c
JOIN source_item s ON s.id = c.source_item_id
LEFT JOIN entity e ON e.id = c.counterparty_entity_id
WHERE c.user_id = :user_id
  AND c.status = 'open'
ORDER BY
  c.due_at IS NULL,       -- dated commitments first; undated sink to the bottom
  c.due_at ASC,
  c.confidence DESC;
