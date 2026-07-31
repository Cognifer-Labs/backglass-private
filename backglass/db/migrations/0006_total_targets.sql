-- Total targets: a lifetime accumulator alongside cadence and milestone (Phase 10).
-- kind='total' rows set total_count (the number to reach — hours, users, interviews);
-- each checkpoint's delta carries the amount logged, and progress is SUM(delta) over
-- all time, computed on read like every other target (G3, G10). NULL on other kinds.
ALTER TABLE target ADD COLUMN total_count INTEGER;
