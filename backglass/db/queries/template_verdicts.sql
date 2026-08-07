-- Prior verdicts among an item's template siblings, and what came of them.
--
-- The rule layer drops a new sibling only when the shape has proved itself inert. What
-- counts as proof changed on 2026-08-07, for the reason the sender rule changed two
-- days earlier and by the same measurement: "one keep, ever, disqualifies" is the right
-- instinct against the wrong signal. College marketing is KEPT — it is written to look
-- like a deadline — so the shapes doing the most damage could never qualify, and their
-- siblings were re-triaged and re-extracted forever. On the owner's ledger, 18 template
-- shapes with zero productive siblings account for 98 completed extractions that
-- produced nothing, at a measured 17.5c of imputed spend each.
--
--   dropped    prior siblings the model or a rule dropped. Unchanged.
--   settled    prior siblings that were kept, ran extraction to completion, and
--              produced nothing, on mail carrying a broadcast marker. Evidence, at
--              last, of the expensive mistake this rule exists to stop repeating.
--   productive prior siblings that yielded anything at all — a commitment, an
--              engagement, a fact, a citation, a goal checkpoint. The disqualifier.
--   unsettled  prior siblings kept and not yet answered: pending, or parked by a
--              failure. Also disqualifying, because an unanswered keep is an open
--              question rather than a resolved nothing, and the next extract pass may
--              still turn it into a commitment.
--
-- The broadcast marker is what keeps `settled` away from a human correspondent. A
-- colleague who writes the same shape of note five times accumulates barren keeps too;
-- personal mail does not carry an unsubscribe footer.
--
-- Params: :user_id, :template_hash, :id
WITH sibling AS (
  SELECT
    s.id,
    s.triage_verdict,
    (s.extraction_version IS NOT NULL) AS extracted,
    (lower(s.body_text) LIKE '%unsubscribe%'
       OR lower(s.body_text) LIKE '%email preferences%'
       OR lower(s.body_text) LIKE '%opt out%') AS bulk,
    EXISTS (
      SELECT 1 FROM commitment c WHERE c.source_item_id = s.id
      UNION ALL SELECT 1 FROM engagement e WHERE e.source_item_id = s.id
      UNION ALL SELECT 1 FROM fact f WHERE f.source_item_id = s.id
      UNION ALL SELECT 1 FROM commitment_evidence ce WHERE ce.source_item_id = s.id
      UNION ALL SELECT 1 FROM checkpoint ck WHERE ck.source_item_id = s.id
    ) AS produced
  FROM source_item s
  WHERE s.user_id = :user_id
    AND s.template_hash = :template_hash
    AND s.id != :id
    AND s.triage_verdict IS NOT NULL
)
SELECT
  COALESCE(SUM(triage_verdict = 'drop'), 0)                                AS dropped,
  COALESCE(SUM(triage_verdict = 'keep'), 0)                                AS kept,
  COALESCE(SUM(produced), 0)                                               AS productive,
  COALESCE(SUM(triage_verdict = 'keep' AND NOT extracted), 0)              AS unsettled,
  COALESCE(SUM(triage_verdict = 'keep' AND extracted
               AND NOT produced AND bulk), 0)                              AS settled
FROM sibling;
