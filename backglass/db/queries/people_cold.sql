-- Going-cold candidates: curated profiles only (role, org, or tags set). The
-- threshold comparison happens in people/touch.py so the levels live next to
-- their settings; this query just returns the last interaction per profile,
-- with enough source columns for a brief line's provenance.
--
-- "Last interaction" is the newest of two kinds of evidence, unioned rather than
-- chosen: a commitment extracted from a message, and a touchpoint the owner
-- recorded by hand (migration 0023). Both carry a source item, so the provenance
-- columns below are the same shape either way and every reader — the brief line,
-- the People chip, the person page — keeps working without knowing which it got.
--
-- Params: :user_id
SELECT
  e.id, e.canonical_name, e.role, e.org, e.tags_json, e.touch_every_days,
  s.occurred_at AS last_touch_at,
  s.id          AS source_item_id,
  s.source, s.external_id AS source_external_id,
  s.occurred_at AS source_occurred_at,
  s.title       AS source_title
FROM entity e
LEFT JOIN source_item s ON s.id = (
  SELECT source_item_id FROM (
    SELECT c.source_item_id AS source_item_id, si.occurred_at AS occurred_at
      FROM commitment c
      JOIN source_item si ON si.id = c.source_item_id
     WHERE c.counterparty_entity_id = e.id
    UNION ALL
    SELECT t.source_item_id AS source_item_id, t.occurred_at AS occurred_at
      FROM touchpoint t
     WHERE t.entity_id = e.id AND t.user_id = e.user_id
  )
  ORDER BY datetime(occurred_at) DESC LIMIT 1
)
WHERE e.user_id = :user_id
  AND e.kind = 'person'
  AND (e.role IS NOT NULL OR e.org IS NOT NULL OR e.tags_json != '[]')
ORDER BY last_touch_at IS NULL DESC, last_touch_at ASC;
