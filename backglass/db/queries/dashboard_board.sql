-- docs/06 §Panels, Commitments: "Board grouped by status, swimlanes by counterparty."
--
-- Everything open and at or above the confidence threshold. Below it belongs to the
-- review queue, not the board — CLAUDE.md rule 2, and putting a guess on the board next
-- to a fact is exactly the confusion the queue exists to prevent.
--
-- `source_title` is the document's subject and `evidence_quote` the sentence inside it
-- the claim rests on; both are shown when a card is expanded (docs/11 §3 step 3: "full
-- extracted text, source link, estimate, linked goal"). `mention_count` is how many
-- documents have said this — a promise restated three times reads differently from one
-- said once, and until migration 0013 the restatements were discarded by the dedup pass.
--
-- Params: :user_id, :today, :confidence_threshold
SELECT
  c.id, c.direction, c.what, c.due_at, c.confidence, c.estimated_minutes,
  c.estimate_source, c.rollover_count, c.goal_id, c.source_item_id,
  e.canonical_name AS counterparty,
  s.source, s.external_id AS source_external_id, s.occurred_at AS source_occurred_at,
  s.title AS source_title,
  g.title AS goal_title,
  (SELECT ce.quote FROM commitment_evidence ce
    WHERE ce.commitment_id = c.id AND ce.quote IS NOT NULL
    ORDER BY ce.id LIMIT 1)                      AS evidence_quote,
  (SELECT COUNT(*) FROM commitment_evidence ce
    WHERE ce.commitment_id = c.id)               AS mention_count,
  CASE
    WHEN c.due_at IS NULL                      THEN 'open'
    WHEN date(c.due_at) <  date(:today)        THEN 'overdue'
    WHEN date(c.due_at) =  date(:today)        THEN 'due_today'
    WHEN date(c.due_at) <= date(:today, '+2 days') THEN 'slipping'
    ELSE 'open'
  END AS state
FROM commitment c
JOIN source_item s ON s.id = c.source_item_id
LEFT JOIN entity e ON e.id = c.counterparty_entity_id
LEFT JOIN goal g ON g.id = c.goal_id
WHERE c.user_id = :user_id
  AND c.status = 'open'
  AND c.confidence >= :confidence_threshold
ORDER BY
  CASE WHEN c.direction = 'i_owe' THEN 0 ELSE 1 END,
  COALESCE(e.canonical_name, 'zzz'),
  c.due_at IS NULL,
  date(c.due_at) ASC;
