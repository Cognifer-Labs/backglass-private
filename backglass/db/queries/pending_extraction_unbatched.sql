-- pending_extraction minus items already riding a live batch, so the 30-minute sync
-- and an overnight batch never pay for the same extraction twice.
--
-- "Live" means submitted within the last 26 hours: the Batches API guarantees
-- completion within 24, so past that window the batch is presumed dead and its items
-- reappear here automatically — the degrade valve needs no operator (CLAUDE.md rule 5).
--
-- A manual item is never pending. `web/actions.py::quick_add` writes the owner's own
-- typed sentence as a source item and inserts the commitment it names in the same
-- breath, so there is nothing left for a model to find — and the stamp it writes,
-- 'manual', is not a prompt version, so it can never appear in :compatible_versions and
-- the row stayed pending forever. Every prompt bump re-read the owner's own words and
-- wrote another commitment beside the one they had already typed.
--
-- Measured on the owner's ledger, 2026-08-27: **37 manual source items, not one of them
-- still carrying the 'manual' stamp** — all overwritten by @9 or @10 — and **81
-- commitments hanging off 34 of them** where the owner typed 34. Two items had grown
-- seven commitments each. 39 of the surplus are open right now. The pair that exposed it
-- was 639, typed at 23:59 with a due date of Sep 3, and 642, produced from the same row
-- twenty-two minutes later with a due date of Sep 10 read out of prose in the note: the
-- model's reading of the owner's sentence outranked the owner's own field.
--
-- Excluded by `source`, not by the stamp. Keying on `extraction_version = 'manual'`
-- would only hold until the next bump overwrites it, which is precisely how this got
-- here.
--
-- Params: :user_id, :compatible_versions (comma-joined stamps), :cutoff  (ISO timestamp, now - 26h)
SELECT si.id, si.source, si.external_id, si.occurred_at, si.author, si.title,
       si.body_text, si.raw_json
FROM source_item si
WHERE si.user_id = :user_id
  AND si.triage_verdict = 'keep'
  AND si.source <> 'manual'
  -- :compatible_versions is every stamp that counts as done — the current one plus the
  -- prompt's `compatible:` list — comma-joined by the caller. instr() with commas on
  -- both sides so `@9` can never match inside `@19`.
  AND (si.extraction_version IS NULL
       OR instr(',' || :compatible_versions || ',', ',' || si.extraction_version || ',') = 0)
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
