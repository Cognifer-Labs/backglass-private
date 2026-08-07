-- All triaged mail-shaped items, for learned-noise mining. Grouped in Python
-- (backglass/extract/noise.py) rather than SQL because sender parsing must match
-- rules._address — email.utils.parseaddr — which SQL cannot express.
--
-- Three derived columns carry the evidence the mining rule needs:
--
--   produced      Did ANYTHING ever come of this item — a commitment, an engagement,
--                 a fact, a citation, or a goal checkpoint. This is the disqualifier:
--                 a sender that has ever produced a record is never noise, whatever
--                 its volume. Checkpoint is in the union because a sender whose mail
--                 only ever moves a goal forward would otherwise read as pure noise.
--   extracted     Did the expensive pass actually run and finish. `extraction_version`
--                 is stamped only after a successful extraction (sync.py) and is left
--                 unset when an item is parked, so this distinguishes "the model looked
--                 and found nothing" from "the model never looked" and from "extraction
--                 failed" — the last of which is evidence of a bug, never of noise.
--   bulk          Does the item carry a marker only broadcast mail carries. This is
--                 what keeps the barren-keep evidence class away from human
--                 correspondents: a quiet colleague whose first five mails are
--                 informational accumulates barren keeps too, and dropping their sixth
--                 is the exact false negative triage.md's keep-bias exists to prevent.
--                 Personal mail does not carry an unsubscribe footer.
--
-- Params: :user_id
SELECT
  s.id,
  s.author,
  s.title,
  s.triage_verdict,
  s.triage_reason,
  s.occurred_at,
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
  AND s.triage_verdict IS NOT NULL
  AND s.author LIKE '%@%';
