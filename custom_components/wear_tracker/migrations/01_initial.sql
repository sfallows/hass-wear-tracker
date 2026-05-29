-- 01_initial.sql — v0.1 schema for hass-wear-tracker.
-- All statements use IF NOT EXISTS so a partial-apply can be safely re-run.
-- schema_version is bumped by the migration runner in storage.py, not here.

CREATE TABLE IF NOT EXISTS entity_meta (
    id              INTEGER PRIMARY KEY,
    unique_id       TEXT UNIQUE,
    entity_id       TEXT NOT NULL UNIQUE,
    domain          TEXT NOT NULL,
    friendly_name   TEXT,
    manufacturer    TEXT,
    model           TEXT,
    rated_hours     REAL,
    rated_cycles    INTEGER,
    tracking_since  INTEGER NOT NULL,
    disabled        INTEGER NOT NULL DEFAULT 0,
    debounce_s      REAL NOT NULL DEFAULT 2.0
);
CREATE INDEX IF NOT EXISTS ix_entity_meta_active ON entity_meta(id) WHERE disabled = 0;

CREATE TABLE IF NOT EXISTS transitions (
    id              INTEGER PRIMARY KEY,
    entity_meta_id  INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    from_state      TEXT NOT NULL,
    to_state        TEXT NOT NULL,
    raw_from        TEXT,
    raw_to          TEXT,
    delta_s         REAL NOT NULL,
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_transitions_entity_ts ON transitions(entity_meta_id, ts);
CREATE INDEX IF NOT EXISTS ix_transitions_ts ON transitions(ts);

CREATE TABLE IF NOT EXISTS daily_summary (
    entity_meta_id  INTEGER NOT NULL,
    day             TEXT NOT NULL,
    on_seconds      REAL NOT NULL DEFAULT 0,
    connected_seconds REAL NOT NULL DEFAULT 0,
    cycles          INTEGER NOT NULL DEFAULT 0,
    drops           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (entity_meta_id, day),
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS summary (
    entity_meta_id    INTEGER PRIMARY KEY,
    lifetime_seconds  REAL NOT NULL DEFAULT 0,
    connected_seconds REAL NOT NULL DEFAULT 0,
    lifetime_cycles   INTEGER NOT NULL DEFAULT 0,
    connection_drops  INTEGER NOT NULL DEFAULT 0,
    last_state        TEXT,
    last_change_ts    INTEGER,
    updated_ts        INTEGER NOT NULL,
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
