-- The reverse question: what did this document produce?
--
-- A commitment reaches this list two ways, and both matter. It either names the item as
-- its origin (`commitment.source_item_id`), or a later reading of this item restated it
-- and citation rows record that (`commitment_evidence`, migration 0013). Before those
-- citations existed the second kind was invisible: the dedup pass recognised the
-- restatement and discarded it, so a thread that repeated a promise looked, from every
-- message after the first, like a document that produced nothing.
--
-- `cited_as` distinguishes them so the page can say which it is rather than implying
-- every row was born here.
--
-- Params: :user_id, :id
SELECT
  c.id, c.direction, c.what, c.due_at, c.confidence, c.status,
  e.canonical_name AS counterparty,
  ce.quote         AS quote,
  ce.kind          AS cited_as,
  CASE WHEN c.source_item_id = :id THEN 1 ELSE 0 END AS is_origin
FROM commitment_evidence ce
JOIN commitment c ON c.id = ce.commitment_id
LEFT JOIN entity e ON e.id = c.counterparty_entity_id
WHERE ce.user_id = :user_id AND ce.source_item_id = :id
ORDER BY is_origin DESC, c.id;
