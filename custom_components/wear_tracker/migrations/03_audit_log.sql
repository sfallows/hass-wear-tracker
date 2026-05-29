-- 03_audit_log.sql — v0.4 schema: admin-action audit trail.
-- entity_meta_id is nullable (ON DELETE SET NULL) so audit rows survive a purge.

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    entity_meta_id  INTEGER,
    action          TEXT NOT NULL,           -- 'reset' | 'set_rated' | 'disable' | 'recompute' | 'purge' | 'purge_all'
    ts              INTEGER NOT NULL,
    payload         TEXT,                    -- JSON: prior/new values, entity_id/unique_id snapshot
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_log_entity_ts ON audit_log(entity_meta_id, ts);
