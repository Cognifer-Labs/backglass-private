-- How many kept items are waiting for an extraction nobody is buying.
--
-- The predicate is pending_extraction_unbatched's, counted instead of selected, and that
-- is the whole point: when the spend cap has paused extraction, the number the owner
-- reads on the Sources panel has to be the number the next un-degraded run would work
-- through. A looser count (every 'keep' with a NULL version) would keep counting items
-- a live batch is already paying for, and drift from the queue it claims to describe.
--
-- Params: :user_id, :extraction_version, :cutoff  (ISO timestamp, now - 26h)
SELECT COUNT(*) AS stranded
FROM source_item si
WHERE si.user_id = :user_id
  AND si.triage_verdict = 'keep'
  -- Mirrors pending_extraction_unbatched's manual exclusion, and has to: the whole
  -- contract of this file is that the number the owner reads is the number the next
  -- un-degraded run would work through. Manual items are no longer in that queue.
  AND si.source <> 'manual'
  AND (si.extraction_version IS NULL OR si.extraction_version != :extraction_version)
  AND NOT EXISTS (
    SELECT 1
    FROM model_batch_item mbi
    JOIN model_batch mb ON mb.batch_id = mbi.batch_id
    WHERE mbi.source_item_id = si.id
      AND mb.status = 'submitted'
      AND mb.created_at >= :cutoff
  );
