-- 04_summary_reset_ts.sql — record the last wear_tracker.reset timestamp per
-- summary row so recompute only replays history at/after a reset and cannot
-- resurrect pre-reset counters (see DESIGN.md §5). NULL = never reset.
--
-- SQLite's ALTER TABLE ADD COLUMN cannot be guarded with IF NOT EXISTS, so a
-- bare ADD is not re-runnable: a crash between applying it and stamping
-- schema_version would make the next boot re-run it and fail with 'duplicate
-- column name', bricking startup. Rebuild the table instead (like 05) so the
-- migration is idempotent. The runner wraps this in one transaction with the
-- schema_version stamp and disables foreign_keys, so the DROP below cannot
-- cascade history away and a partial apply rolls back cleanly.

CREATE TABLE summary_new (
    entity_meta_id    INTEGER PRIMARY KEY,
    lifetime_seconds  REAL NOT NULL DEFAULT 0,
    connected_seconds REAL NOT NULL DEFAULT 0,
    lifetime_cycles   INTEGER NOT NULL DEFAULT 0,
    connection_drops  INTEGER NOT NULL DEFAULT 0,
    last_state        TEXT,
    last_change_ts    INTEGER,
    updated_ts        INTEGER NOT NULL,
    reset_ts          INTEGER,
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);
INSERT INTO summary_new (
    entity_meta_id, lifetime_seconds, connected_seconds, lifetime_cycles,
    connection_drops, last_state, last_change_ts, updated_ts
)
SELECT entity_meta_id, lifetime_seconds, connected_seconds, lifetime_cycles,
    connection_drops, last_state, last_change_ts, updated_ts
FROM summary;
DROP TABLE summary;
ALTER TABLE summary_new RENAME TO summary;
