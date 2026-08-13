-- Interaction timeline for one person. Three legs:
--   via='commitment' — source items behind commitments with this counterparty
--   via='touch'      — touches the owner recorded by hand (migration 0023)
--   via='mention'    — source items whose author matches one of the aliases, that
--                      did not already appear in the first leg
--
-- The touch leg is here because the banner and this panel must not disagree. A person
-- whose whole history is one dinner read "0 days since last touch" above a timeline
-- saying "No interactions on record", which is the two-surfaces-disagree failure this
-- codebase keeps paying for. A recorded touch is an interaction; it belongs in the list
-- of them.
-- Every row is a source_item, so every timeline entry carries provenance by
-- construction (CLAUDE.md rule 1).
--
-- Params: :user_id, :entity_id
SELECT
  s.id AS source_item_id, s.source, s.external_id AS source_external_id,
  s.occurred_at AS source_occurred_at, datetime(s.occurred_at) AS at_utc,
  s.title AS source_title, s.author,
  'commitment' AS via, c.what
FROM commitment c
JOIN source_item s ON s.id = c.source_item_id
WHERE c.user_id = :user_id AND c.counterparty_entity_id = :entity_id
UNION ALL
SELECT
  s.id, s.source, s.external_id, s.occurred_at, datetime(s.occurred_at), s.title, s.author,
  'touch', t.note
FROM touchpoint t
JOIN source_item s ON s.id = t.source_item_id
WHERE t.user_id = :user_id AND t.entity_id = :entity_id
UNION ALL
SELECT
  s.id, s.source, s.external_id, s.occurred_at, datetime(s.occurred_at), s.title, s.author,
  'mention', NULL
FROM source_item s
WHERE s.user_id = :user_id
  AND s.author IS NOT NULL
  AND EXISTS (SELECT 1 FROM entity e, json_each(e.aliases_json) a
              WHERE e.id = :entity_id
                AND instr(lower(s.author), lower(a.value)) > 0)
  AND s.id NOT IN (SELECT c2.source_item_id FROM commitment c2
                   WHERE c2.counterparty_entity_id = :entity_id)
-- at_utc, not the raw column: occurred_at keeps each source's own offset, so text
-- order interleaves the two zones wrongly. A compound SELECT can only order by an
-- output name, hence the extra column.
ORDER BY at_utc DESC
LIMIT 50;
