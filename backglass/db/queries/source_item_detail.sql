-- One raw item, for the source page that provenance links land on.
--
-- The whole product is "a commitment ledger with documents as evidence", and until this
-- query existed there was no way to read the document half. Every non-Gmail provenance
-- link in the brief pointed at /source/<something> and got a 404, so "check the source"
-- was advice the system could not honour for the sources the owner actually has.
--
-- Triage fields come along deliberately: the most useful question on this page is often
-- "why did nothing come out of this?", and the answer is the verdict plus its reason.
--
-- Params: :user_id, :id
SELECT
  s.id, s.source, s.external_id, s.author, s.title, s.body_text,
  s.occurred_at, s.fetched_at, s.triage_verdict, s.triage_reason,
  s.extraction_version,
  LENGTH(s.body_text) AS body_length
FROM source_item s
WHERE s.user_id = :user_id AND s.id = :id;
