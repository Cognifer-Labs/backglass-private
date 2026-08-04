-- How many ingested items are still waiting to be read at all.
--
-- Sibling of stranded_extractions.sql, and for the same reason: the number the owner is
-- shown has to be the number the next un-paused run would work through, so the predicate
-- is the pipeline's own (pending_triage.sql) rather than a looser one.
--
-- It exists because the stranded count is structurally zero for a run a usage window
-- stopped during triage. That count opens with `triage_verdict = 'keep'`, and an item
-- triage never reached has no verdict at all — so the Sources panel reported "0 items
-- waiting" at exactly the moment the most of the ledger was missing, which reads as
-- reassurance. A count of the wrong population is worse than no count.
--
-- Params: :user_id
SELECT COUNT(*) AS untriaged
FROM source_item
WHERE user_id = :user_id
  AND (triage_verdict IS NULL OR triage_verdict = 'unclassified');
