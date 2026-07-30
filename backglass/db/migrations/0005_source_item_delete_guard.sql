-- Close the gap the Phase 6 verifier found: 0002 made source_item immutable
-- against UPDATE, but DELETE was unguarded while docs/03 promises raw items are
-- kept forever. The one legitimate deleter is the docs/08 D6 boundary purge, so
-- deletion goes through a gate: a single-row table the purge opens inside its
-- transaction and always closes. Anything else hitting DELETE aborts.
CREATE TABLE purge_gate (
  id   INTEGER PRIMARY KEY CHECK (id = 1),
  open INTEGER NOT NULL DEFAULT 0
);
INSERT INTO purge_gate (id, open) VALUES (1, 0);

CREATE TRIGGER source_item_no_delete
BEFORE DELETE ON source_item
WHEN (SELECT open FROM purge_gate WHERE id = 1) = 0
BEGIN
  SELECT RAISE(ABORT, 'source_item rows are kept forever (docs/03); only the boundary purge (docs/08 D6) may delete');
END;
