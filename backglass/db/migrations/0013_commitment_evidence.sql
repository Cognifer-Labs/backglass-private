-- Sentence-level provenance, stored instead of discarded.
--
-- The extraction schema has always asked the model for `evidence` — "the exact sentence,
-- verbatim... CLAUDE.md rule 1: a claim with no provenance does not ship, and this is the
-- provenance at the sentence level" (backglass/extract/schemas.py). The write layer then
-- dropped it on the floor, and the review queue rendered the email *subject* in its place
-- under a comment citing docs/11 §4's "the exact source sentence beneath it in quotes".
-- A subject line is not evidence: "Fall housing" does not tell the owner whether the
-- thing they are being asked to accept is really in that mail.
--
-- Why a table rather than a column on `commitment`: an obligation is usually said more
-- than once. Today the dedup step in extract/commitments.py recognises the restatement
-- and throws it away, so the second and third sighting leave no trace, and when dedup
-- misses instead, the ledger grows a near-duplicate row. Citations belong many-to-one so
-- a restatement can strengthen the commitment it matched — and so the source page can
-- ask the reverse question, "what did this document produce?"
--
-- `kind` distinguishes the first sighting (`original`) from a later restatement
-- (`restated`) and from the owner's own typed words on a quick-add (`manual`).
--
-- UNIQUE (user_id, commitment_id, source_item_id) is what keeps rule 3 true: re-reading
-- an item the ledger has already seen conflicts instead of writing, so a second sync with
-- no upstream change still writes nothing.
--
-- Backfill: every existing commitment gets its `original` citation from the
-- source_item_id it already carries, with a NULL quote — the sentence was never stored
-- and cannot be invented. Uniform shape matters more than a complete one here: every
-- commitment has at least one citation row from this migration forward, so the read path
-- has no "some have none" branch, and a NULL quote correctly reads as "we know which
-- document, not which sentence".

CREATE TABLE commitment_evidence (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  commitment_id  INTEGER NOT NULL REFERENCES commitment(id),
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  quote          TEXT,                                    -- verbatim; NULL = document only
  kind           TEXT    NOT NULL DEFAULT 'original',     -- original|restated|manual
  seen_at        TEXT    NOT NULL,
  UNIQUE (user_id, commitment_id, source_item_id)
);

CREATE INDEX idx_commitment_evidence ON commitment_evidence(user_id, commitment_id, id);
CREATE INDEX idx_evidence_by_source  ON commitment_evidence(user_id, source_item_id);

INSERT INTO commitment_evidence (user_id, commitment_id, source_item_id, quote, kind, seen_at)
SELECT user_id, id, source_item_id, NULL, 'original', created_at FROM commitment;
