-- pending_extraction minus items already riding a live batch, so the 30-minute sync
-- and an overnight batch never pay for the same extraction twice.
--
-- "Live" means submitted within the last 26 hours: the Batches API guarantees
-- completion within 24, so past that window the batch is presumed dead and its items
-- reappear here automatically — the degrade valve needs no operator (CLAUDE.md rule 5).
--
-- Params: :user_id, :extraction_version, :cutoff  (ISO timestamp, now - 26h)
SELECT si.id, si.source, si.external_id, si.occurred_at, si.author, si.title,
       si.body_text, si.raw_json
FROM source_item si
WHERE si.user_id = :user_id
  AND si.triage_verdict = 'keep'
  AND (si.extraction_version IS NULL OR si.extraction_version != :extraction_version)
  AND NOT EXISTS (
    SELECT 1
    FROM model_batch_item mbi
    JOIN model_batch mb ON mb.batch_id = mbi.batch_id
    WHERE mbi.source_item_id = si.id
      AND mb.status = 'submitted'
      AND mb.created_at >= :cutoff
  )
-- datetime(), not the bare column: occurred_at keeps each source's own UTC offset, so
-- text order inverts across the owner's two zones. Supersession applies oldest-first.
ORDER BY datetime(si.occurred_at) ASC;
