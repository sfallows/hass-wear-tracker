-- 05_platform_and_disabled_reason.sql
-- Two related identity fixes (DESIGN.md §4):
--   * HA unique_ids are only unique per (platform, domain), so the old
--     `unique_id TEXT UNIQUE` constraint wrongly merged two different entities
--     that happened to share a unique_id string. Add a `platform` column and
--     key identity on the composite (platform, domain, unique_id) instead.
--   * `disabled_reason` distinguishes a soft-delete on registry removal
--     ('removed', which resumes on re-registration) from a deliberate
--     wear_tracker.disable ('user', which stays disabled across restarts).
--
-- Relaxing the inline UNIQUE(unique_id) constraint requires rebuilding the
-- table. The migration runner disables foreign_keys around all migrations so the
-- DROP below doesn't cascade-delete the history tables that FK to
-- entity_meta(id); surrogate ids are preserved so the references stay valid.

BEGIN;
CREATE TABLE entity_meta_new (
    id              INTEGER PRIMARY KEY,
    unique_id       TEXT,
    entity_id       TEXT NOT NULL UNIQUE,
    domain          TEXT NOT NULL,
    platform        TEXT,
    friendly_name   TEXT,
    manufacturer    TEXT,
    model           TEXT,
    rated_hours     REAL,
    rated_cycles    INTEGER,
    tracking_since  INTEGER NOT NULL,
    disabled        INTEGER NOT NULL DEFAULT 0,
    disabled_reason TEXT,
    debounce_s      REAL NOT NULL DEFAULT 2.0
);
INSERT INTO entity_meta_new (
    id, unique_id, entity_id, domain, platform, friendly_name, manufacturer,
    model, rated_hours, rated_cycles, tracking_since, disabled, disabled_reason,
    debounce_s
)
SELECT id, unique_id, entity_id, domain, NULL, friendly_name, manufacturer,
    model, rated_hours, rated_cycles, tracking_since, disabled, NULL, debounce_s
FROM entity_meta;
DROP TABLE entity_meta;
ALTER TABLE entity_meta_new RENAME TO entity_meta;
CREATE INDEX ix_entity_meta_active ON entity_meta(id) WHERE disabled = 0;
-- NULLs compare distinct in a UNIQUE index, so legacy rows without a unique_id
-- (or platform) are still allowed to coexist, matched by entity_id instead.
CREATE UNIQUE INDEX ix_entity_meta_identity ON entity_meta(platform, domain, unique_id);
COMMIT;
