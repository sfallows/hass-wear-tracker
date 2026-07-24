-- 04_summary_reset_ts.sql — record the last wear_tracker.reset timestamp per
-- summary row so recompute only replays history at/after a reset and cannot
-- resurrect pre-reset counters (see DESIGN.md §5). NULL = never reset.

ALTER TABLE summary ADD COLUMN reset_ts INTEGER;
