-- The handful of messages immediately before one item, in the same conversation.
--
-- Extraction reads a single source_item, which is right for mail and wrong for chat. On
-- the owner's real ledger, triage keeps messages like "ye", "Sure", "5 rn", "6" — each
-- one costs a full extraction call and none of them means anything alone. "6" is a time
-- somebody is agreeing to, and the agreement is in the message above it.
--
-- Context only. The focal item stays the citation and the evidence sentence still has to
-- come from it (CLAUDE.md rule 1) — these rows are here so the model can resolve what
-- "6" refers to, not so it can quote them.
--
-- The conversation key is whatever the source uses to group: iMessage writes `chat` for a
-- group and `handle` for a one-to-one, Instagram writes `thread`, Gmail writes a
-- `threadId`. COALESCE over all of them keeps this one query rather than one per source,
-- and an item whose source has no such key simply matches nothing and gets no context.
--
-- Bounded by `:limit` rows, and the caller bounds the characters as well: context is paid
-- for on every extraction call, so it earns its place by being small.
--
-- Ordered by occurred_at as text and NOT through datetime(): these timestamps carry the
-- sender's own offset, and normalising to UTC first reorders an evening message across
-- its own day boundary (tasks/lessons.md, 2026-08-01). Within one conversation the
-- offsets are effectively constant, so the raw ordering is the true one.
--
-- Params: :user_id, :source_item_id, :limit
WITH focal AS (
  SELECT
    source,
    occurred_at,
    COALESCE(
      NULLIF(json_extract(raw_json, '$.chat'), ''),
      NULLIF(json_extract(raw_json, '$.thread'), ''),
      NULLIF(json_extract(raw_json, '$.threadId'), ''),
      NULLIF(json_extract(raw_json, '$.handle'), '')
    ) AS conversation
  FROM source_item
  WHERE user_id = :user_id AND id = :source_item_id
)
SELECT
  s.author,
  s.body_text,
  s.occurred_at,
  json_extract(s.raw_json, '$.is_from_me') AS is_from_me
FROM source_item s, focal
WHERE s.user_id = :user_id
  AND s.source = focal.source
  AND focal.conversation IS NOT NULL
  AND COALESCE(
        NULLIF(json_extract(s.raw_json, '$.chat'), ''),
        NULLIF(json_extract(s.raw_json, '$.thread'), ''),
        NULLIF(json_extract(s.raw_json, '$.threadId'), ''),
        NULLIF(json_extract(s.raw_json, '$.handle'), '')
      ) = focal.conversation
  AND s.id != :source_item_id
  AND s.occurred_at <= focal.occurred_at
  AND s.body_text IS NOT NULL
ORDER BY s.occurred_at DESC, s.id DESC
LIMIT :limit;
