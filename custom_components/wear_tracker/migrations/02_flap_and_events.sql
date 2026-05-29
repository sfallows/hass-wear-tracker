-- 02_flap_and_events.sql — v0.3 schema: anomaly debounce + flap baselines.
-- IF NOT EXISTS so a partial-apply can be safely re-run.

CREATE TABLE IF NOT EXISTS events_fired (
    entity_meta_id  INTEGER NOT NULL,
    event_kind      TEXT NOT NULL,           -- 'flap_anomaly' | 'connection_anomaly' | 'wear_critical'
    discriminator   TEXT NOT NULL,           -- e.g. 'flap', 'connection', 'hours:90'
    fired_ts        INTEGER NOT NULL,
    PRIMARY KEY (entity_meta_id, event_kind, discriminator),
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_events_fired_ts ON events_fired(fired_ts);

CREATE TABLE IF NOT EXISTS flap_baseline (
    entity_meta_id  INTEGER NOT NULL,
    hour_of_day     INTEGER NOT NULL,        -- 0-23 (UTC)
    flap_rate       REAL NOT NULL,           -- transitions/hour, mean for this hour over the window
    unavail_rate    REAL NOT NULL,           -- drops/hour, same window
    updated_ts      INTEGER NOT NULL,
    PRIMARY KEY (entity_meta_id, hour_of_day),
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);
