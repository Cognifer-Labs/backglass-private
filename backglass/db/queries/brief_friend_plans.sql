-- Friend plans: open commitments whose evidence came from Instagram (docs/07
-- §Instagram). Category is derived from provenance, not stored — the commitment table
-- has no category column and the queries are known in advance, so a JOIN is the whole
-- feature.
--
-- Both lanes match: the export connector writes source='instagram', the live lane
-- 'instagram:live'.
--
-- Undated plans are included ("dinner soon?" carries no due_at) — nothing else in the
-- brief would ever surface them. Dated plans sort first, soonest first.
--
-- Special events (birthdays and the like) are *not* filtered here: SQLite has no rich
-- regex, and the special-event test lives in one place, brief/daily.py, where both the
-- demotion filter and this section can share it.
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
  AND (s.source = 'instagram' OR s.source LIKE 'instagram:%')
  AND (c.due_at IS NULL OR date(c.due_at) <= date(:horizon))
  AND c.confidence >= :confidence_threshold
ORDER BY (c.due_at IS NULL) ASC, date(c.due_at) ASC, c.confidence DESC;
