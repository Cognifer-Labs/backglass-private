-- docs/05 §8 Needs review, for engagements. The other half of CLAUDE.md rule 2.
--
-- The rule has two clauses: a low-confidence extraction stays out of the brief as fact,
-- AND it goes to a review queue. Engagements honoured the first from the day they
-- landed — brief_engagements.sql filters on confidence — and silently dropped the
-- second, because every review surface read FROM commitment. The post-processing pass
-- counted `review_queue` and the CLI printed the number, so the run reported a queue
-- entry that existed nowhere. A guess the owner can never see is worse than one the
-- system never made: it is invisible work claimed as done.
--
-- Deliberately a second query rather than a UNION into brief_needs_review.sql. The two
-- records answer different questions — "did you promise this?" against "is this a real
-- plan?" — and the columns a reviewer needs differ (a direction and a due date against a
-- guest list and a start time). Fusing them would mean NULL-padding both sides and a
-- caller that switches on a discriminator column anyway.
--
-- Bounded below, like the Plans section above it. A guess about a plan that was meant to
-- happen last year is not a question worth asking every morning forever; commitments need
-- no equivalent because a commitment has no date on which it becomes moot.
--
-- Params: :user_id, :confidence_threshold, :floor (ISO date)
SELECT
  e.id,
  e.kind,
  e.what,
  e.starts_at,
  e.location,
  e.status,
  e.confidence,
  e.source_item_id,
  GROUP_CONCAT(p.canonical_name, ', ') AS people,
  -- Same reasoning as the commitment query: accepting or rejecting a guess means
  -- reading the sentence it rests on, not the subject line.
  (SELECT ee.quote FROM engagement_evidence ee
    WHERE ee.engagement_id = e.id AND ee.quote IS NOT NULL
    ORDER BY ee.id LIMIT 1) AS evidence_quote,
  s.source,
  s.external_id AS source_external_id,
  s.occurred_at AS source_occurred_at,
  s.title       AS source_title
FROM engagement e
JOIN source_item s ON s.id = e.source_item_id
LEFT JOIN engagement_person ep
  ON ep.engagement_id = e.id AND ep.user_id = e.user_id
LEFT JOIN entity p ON p.id = ep.entity_id
WHERE e.user_id = :user_id
  AND e.status IN ('proposed', 'confirmed')
  AND e.confidence < :confidence_threshold
  AND (e.starts_at IS NULL OR substr(e.starts_at, 1, 10) >= :floor)
GROUP BY e.id
ORDER BY e.confidence DESC, e.id ASC;
