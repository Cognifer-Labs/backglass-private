-- Per-source on/off switch. A disabled source keeps its credential, cursor, and
-- every stored item — it is a pause, not a removal. The sync skips it, the Sources
-- panel shows it greyed with a Resume action, and the brief's failure section
-- ignores it (a source the owner turned off is not "failing").
ALTER TABLE credential ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
