-- Going-cold candidates: curated profiles only (role, org, or tags set). The
-- threshold comparison happens in people/touch.py so the levels live next to
-- their settings; this query just returns the last interaction per profile,
-- with enough source columns for a brief line's provenance.
--
-- Params: :user_id
SELECT
  e.id, e.canonical_name, e.role, e.org, e.tags_json,
  s.occurred_at AS last_touch_at,
  s.id          AS source_item_id,
  s.source, s.external_id AS source_external_id,
  s.occurred_at AS source_occurred_at,
  s.title       AS source_title
FROM entity e
LEFT JOIN source_item s ON s.id = (
  SELECT c.source_item_id FROM commitment c
  JOIN source_item si ON si.id = c.source_item_id
  WHERE c.counterparty_entity_id = e.id
  ORDER BY si.occurred_at DESC LIMIT 1
)
WHERE e.user_id = :user_id
  AND e.kind = 'person'
  AND (e.role IS NOT NULL OR e.org IS NOT NULL OR e.tags_json != '[]')
ORDER BY last_touch_at IS NULL DESC, last_touch_at ASC;
