# DESIGN.md — hass-wear-tracker

## 1. Overview

`hass-wear-tracker` is a Home Assistant custom integration (HACS-distributed) that records lifetime usage statistics for any HA entity. For each tracked entity it accumulates two independent counters — `connected_hours` (time the entity was reachable) and `lifetime_hours` (subset of connected time the entity was logically "on") — plus cycle counts, connection-drop counts, duty cycle, and wear percentage against a rated lifetime pulled from a community catalog (e.g., Hue White = 25,000 h).

It also computes short-window flap rates and compares them against a 30-day rolling baseline to fire early-warning events for failing devices. Persistence is a self-managed SQLite database under `<config>/wear_tracker/`, independent of HA's `recorder`, so data survives the recorder's 10-day purge and provides full audit-log export. The integration ships with a per-domain bulk-confirm wizard on first install and a Repair Issue prompt for any newly-added trackable entity afterward.

## 2. Architecture

```mermaid
flowchart TD
    HA[HA event bus] -->|state_changed| Coord[WearCoordinator]
    Coord --> SM[StateMachine<br/>3-state per entity]
    SM -->|transition row| Writer[AsyncSqliteWriter<br/>serialized, WAL]
    Writer --> DB[(wear_tracker.db<br/>SQLite)]
    SM -->|on transition| Roll[Rollup engine<br/>summary + flap_rate]
    Coord -->|60s tick| Roll
    Roll --> Sensors[Sensor entities<br/>per tracked entity]
    Roll -->|threshold| Events[wear_critical /<br/>flap_anomaly /<br/>connection_anomaly]
    Roll --> HealthBS[binary_sensor<br/>health_alert]
    Cron[Hourly job] --> Roll
    Cron --> Retention[90d transitions purge<br/>daily_summary roll]
    UI[Config flow / Repair] --> Registry[entity_meta]
    Registry --> Coord
    Catalog[catalog.py] --> Registry
```

A single `WearCoordinator` owner class per config entry — a plain class, **not** `DataUpdateCoordinator`, since this integration is event-driven, not poll-driven. It owns: (a) the per-entity state machines in memory, (b) a single-worker SQLite writer thread (`AsyncSqliteWriter`, all writes serialized off the event loop), (c) the rollup/anomaly engine, which runs every 60 s on the heartbeat and once at startup (each transition separately refreshes the affected sensors), and (d) the sensor platform refresh signal.

State events arrive via `async_track_state_change_event` on the set of tracked entity IDs. Sensors refresh by subscribing to `async_dispatcher_connect(hass, SIGNAL_WEAR_UPDATED, ...)`; the coordinator calls `async_dispatcher_send(hass, SIGNAL_WEAR_UPDATED, meta_id)` after each summary write — the signal carries the surrogate `entity_meta.id`, which survives entity_id renames. The 60 s heartbeat (`async_track_time_interval`) folds in-progress on-time into `summary`, stamps `meta.last_alive`, and drives the rollup.

## 3. Code structure

```
custom_components/wear_tracker/
    __init__.py            # async_setup_entry / unload; bootstraps coordinator + DB
    manifest.json          # domain="wear_tracker", iot_class="calculated", deps=[]
    const.py               # DOMAIN, PLATFORMS, CONF_*, SIGNAL_*, EVENT_*, defaults
    coordinator.py         # WearCoordinator (plain class; not DataUpdateCoordinator)
    state_machine.py       # EntityStateMachine, StateKind enum, derive_logical_state()
    storage.py             # AsyncSqliteWriter, schema migrations, query helpers
    catalog.py             # CATALOG dict + lookup_rated(manufacturer, model)
    registry.py            # TrackedEntity dataclass, load/save entity_meta
    rollup.py              # compute_summary(), compute_flap_rate(), baseline math
    discovery.py           # scan_trackable_entities(), domain bulk-prompt helpers
    config_flow.py         # ConfigFlow + OptionsFlow (UI install, v0.2)
    repairs.py             # async_create_fix_flow for new-device prompts
    sensor.py              # WearSensor subclasses (one per metric)
    binary_sensor.py       # HealthAlertBinarySensor
    services.py            # reset / set_rated / export_log / recompute / disable / purge / purge_all
    services.yaml          # service schemas
    events.py              # fire_wear_critical, fire_flap_anomaly helpers
    diagnostics.py         # async_get_config_entry_diagnostics
    translations/en.json   # UI strings (v0.2+)
    strings.json
    migrations/            # NN_description.sql files; applied in order on startup
tests/
    conftest.py            # pytest-homeassistant-custom-component fixtures
    test_state_machine.py
    test_storage.py
    test_rollup.py
    test_discovery.py
    test_services.py
hacs.json                  # HACS manifest
README.md
DESIGN.md                  # this file
.github/workflows/         # ci.yml, hassfest.yml, release.yml
```

## 4. State machine spec

Three logical states: `DISCONNECTED`, `OFF`, `ON`.

Mapping from a HA `State` (raw state + attributes) → logical state lives in `state_machine.derive_logical_state(state)`. Most domains key on the entity state; climate and water_heater key on an attribute because the entity state is an HVAC *mode*, not an indication of active operation.

| Domain | ON when | OFF when |
|---|---|---|
| light | state == `on` | state == `off` |
| switch | state == `on` | state == `off` |
| fan | state == `on` | state == `off` |
| climate | `attributes.hvac_action ∈ {heating, cooling, drying, fan, preheating, defrosting}` | `attributes.hvac_action ∈ {idle, off}` or state == `off` |
| water_heater | state ∈ {`eco`, `electric`, `gas`, `heat_pump`, `high_demand`, `performance`} | state == `off` |
| cover | state ∈ {`opening`, `closing`} (motor active) | state ∈ {`open`, `closed`} (motor idle) |
| vacuum | state ∈ {`cleaning`, `returning`} | state ∈ {`docked`, `idle`, `paused`} |
| media_player | state ∈ {`playing`, `buffering`} | state ∈ {`on`, `paused`, `idle`, `off`} |
| binary_sensor | state == `on` | state == `off` |

`unavailable`, `unknown`, `None`, missing entity → `DISCONNECTED`.

**Note on climate / water_heater accuracy.** Climate keys on `attributes.hvac_action` rather than the entity state, because the entity state is the HVAC *mode* — a thermostat left in `heat` mode all winter would otherwise read 24 h/day of "on time" even when the compressor is idle. If `hvac_action` is absent on a particular climate entity (some integrations don't expose it), `derive_logical_state` falls back to the mode-based mapping (`heat`, `cool`, `heat_cool`, `auto`, `dry`, `fan_only` → ON) and logs a one-time warning per entity. Water_heater has no `hvac_action` equivalent in core, so the mapping accrues *energized* hours (mode != `off`) rather than active-heating hours; users wanting a true duty cycle should pair with a power sensor.

**Note on cover wear.** Cover entities wear through motor cycles, not by sitting open. The mapping above counts ON only while the motor is moving (`opening`, `closing`); a window left open all day registers zero on-hours. For covers, `lifetime_cycles` is the load-bearing wear signal; `lifetime_hours` is cumulative motor runtime, not "time spent open."

### Transition table (current → next)

| from \ to | DISCONNECTED | OFF | ON |
|---|---|---|---|
| DISCONNECTED | (noop) | accrue 0; reset on-start | accrue 0; **start on-period**; `lifetime_cycles += 1` |
| OFF | `connection_drops += 1`; close connected period | (noop) | close off period; **start on-period**; `lifetime_cycles += 1` |
| ON | `connection_drops += 1`; **close on-period**; close connected period | **close on-period** | (noop) |

Each observed transition writes one row to `transitions` with `(ts, entity_meta_id, from_state, to_state, raw_from, raw_to, delta_s)`. `delta_s` semantics are spelled out in §5.

### Edge cases

- **HA restart while ON.** Seeding is deferred to HA-started (source integrations load *after* us, so seeding at entry setup would observe a spurious `DISCONNECTED` and fabricate a cycle when the real `ON` arrives, and rob this recovery of its credit). At seed time, for each tracked entity whose `summary.last_state` was `ON` and which reads `ON` now, credit the downtime as ON — but only if the offline gap measured at *entry setup* (`_setup_ts − last_alive`) is within `RESTART_GAP_TOLERANCE_S` (2× heartbeat = one missed beat tolerated). Measuring the gap at seed-now would blow the tolerance on slow hosts where boot-to-started is 100-150 s and silently drop the credit that matters most. The credited amount runs `last_alive → seed-now` (or, when a live event bootstrapped the machine mid-seed, `last_alive → that bootstrap ts`, so the credit never overlaps live accrual), hard-capped at `RESTART_CREDIT_MAX_S` (5 min) so a slow boot or seed-retry backoff can't fabricate on-time if the device was toggled while unobserved. Longer gaps are left uncredited — the seed just re-observes the current real state rather than inventing on-time across an unknown downtime. Heartbeat is a single `meta.last_alive` row the coordinator rewrites every 60 s (and once at seed end, so a second restart inside the tolerance window recomputes the gap from this boot rather than re-crediting the same downtime).
- **NTP / wall-clock corrections.** Always compute deltas with `time.monotonic()` for the period being closed, but stamp `ts` with `dt_util.utcnow()` for the audit log. Reject negative deltas (clamp to 0) and log a warning.
- **entity_id rename.** `entity_meta` uses a surrogate `id` (INTEGER PK); all history tables (`transitions`, `daily_summary`, `summary`) FK to it. Identity is the composite **(platform, domain, unique_id)** — the tuple HA actually keeps unique — enforced by a `UNIQUE` index (migration 05); `unique_id` alone is *not* unique across platforms, so keying on it would merge two unrelated devices. `entity_id` is a mutable label column. On `event_entity_registry_updated` with `action="update"` where the `entity_id` changed, `reconcile_rename` moves only the `entity_id` label onto the row that owns that identity — history rows reference `entity_meta.id` and need no rewrite. Legacy rows written before platform tracking have `platform = NULL`; both `upsert` and `reconcile_rename` fall back to a relaxed `(NULL, domain, unique_id)` match, but that fallback is **guarded** — it is refused when the stale `entity_id` still belongs to a live entity, so a `unique_id` string shared across platforms can't let one device seize another's history. **Swap renames** (user batch-renames `light.a → light.b` and `light.b → light.a`, delivered as two independent registry events) collide with the `UNIQUE(entity_id)` constraint mid-update — `reconcile_rename` runs in one transaction, parking the occupant row under a sentinel `entity_id` (`__wt_pending_<id>__`) first so the second event finds its target free and the parked row settles onto it.
- **unavailable → on with no off in between.** Treated as legitimate: open new on-period, increment `lifetime_cycles` by 1 (the device cycled while we couldn't see it; we count the visible re-ignition).
- **Rapid bounces under 2 s.** Recorded as raw transitions but excluded from `lifetime_cycles` count (debounce window, configurable per entity). The transition row is still written for audit and flap-rate.
- **Initial bootstrap.** When an entity is first tracked, its first observed state seeds the machine without writing a cycle or drop.
- **Entity deletion.** On `event_entity_registry_updated` with `action="remove"`, the coordinator first closes the final on/connected period and records the `DISCONNECTED` drop (so `last_state` lands `DISCONNECTED` and the last ON period isn't lost), then sets `disabled = 1` with `disabled_reason = 'removed'` (soft delete) rather than dropping the row — lifetime totals are preserved for audit and for re-registration (e.g., a Zigbee device re-paired with the same IEEE address). Re-registration resumes the paused row **only on a confirmed identity match** ((platform, domain, unique_id)) — never on a bare `entity_id` match, which could belong to a different device that inherited the freed `entity_id`. If a *different* identity claims the freed `entity_id` while the removed row still holds it, that row is retired to a tombstone label (`__wt_retired_<id>__…`; history preserved, still disabled) and the claimant gets a fresh row. `disabled_reason='removed'` auto-resumes on re-pair; a deliberate `wear_tracker.disable` sets `disabled_reason='user'`, which stays sticky across restarts. A live re-pair (or re-create of a previously removed `entity_id`) schedules a config-entry reload so tracking resumes without a full HA restart, in every discovery mode — including the case where a restart between removal and re-pair left the disabled placeholder tracked. Sensor entities are (re)created on that reload. A `wear_tracker.purge` service (v0.4+) hard-deletes the row and cascades history when the user really wants the data gone.
- **Integration uninstall.** Config-entry removal does *not* delete `<config>/wear_tracker/`. Reinstalling picks up where it left off (rows match by identity). Users wanting a clean slate run `wear_tracker.purge_all` (v0.4+) or delete the directory manually.

## 5. Storage schema

SQLite at `<config>/wear_tracker/wear_tracker.db`. Pragmas: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`.

The schema below is the v1.0 end-state. Tables are introduced phase-by-phase via migrations (see **Schema migrations** below): v0.1 creates `entity_meta` / `transitions` / `daily_summary` / `summary` / `meta`; v0.3 adds `events_fired` / `flap_baseline`; v0.4 adds `audit_log`. Post-v0.4 correctness fixes add `summary.reset_ts` (migration 04) and `entity_meta.platform` + `disabled_reason` (migration 05, an identity-key table rebuild). Each table's indexes ship with the table.

```sql
CREATE TABLE entity_meta (
    id              INTEGER PRIMARY KEY,
    unique_id       TEXT,                    -- HA entity_registry unique_id when available (unique only per platform+domain)
    entity_id       TEXT NOT NULL UNIQUE,    -- current entity_id; mutable across renames
    domain          TEXT NOT NULL,
    platform        TEXT,                    -- HA entity_registry platform; part of the identity key
    friendly_name   TEXT,
    manufacturer    TEXT,
    model           TEXT,
    rated_hours     REAL,
    rated_cycles    INTEGER,
    tracking_since  INTEGER NOT NULL,        -- unix seconds
    disabled        INTEGER NOT NULL DEFAULT 0,
    disabled_reason TEXT,                    -- 'removed' (auto-resumes on re-pair) | 'user' (sticky) | NULL
    debounce_s      REAL NOT NULL DEFAULT 2.0
);
CREATE INDEX ix_entity_meta_active ON entity_meta(id) WHERE disabled = 0;
-- Identity is the composite HA keeps unique. NULLs compare distinct in a UNIQUE
-- index, so legacy rows without a unique_id/platform coexist and match by entity_id.
CREATE UNIQUE INDEX ix_entity_meta_identity ON entity_meta(platform, domain, unique_id);

CREATE TABLE transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_meta_id  INTEGER NOT NULL,
    ts              INTEGER NOT NULL,        -- unix seconds UTC
    from_state      TEXT NOT NULL,           -- DISCONNECTED|OFF|ON
    to_state        TEXT NOT NULL,
    raw_from        TEXT,
    raw_to          TEXT,
    delta_s         REAL NOT NULL,           -- monotonic duration of the period just closed
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);
CREATE INDEX ix_transitions_entity_ts ON transitions(entity_meta_id, ts);
CREATE INDEX ix_transitions_ts ON transitions(ts);  -- retention purge (WHERE ts < cutoff)

CREATE TABLE daily_summary (
    entity_meta_id  INTEGER NOT NULL,
    day             TEXT NOT NULL,           -- YYYY-MM-DD (UTC; display layer converts to local)
    on_seconds      REAL NOT NULL DEFAULT 0,
    connected_seconds REAL NOT NULL DEFAULT 0,
    cycles          INTEGER NOT NULL DEFAULT 0,
    drops           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (entity_meta_id, day),
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);

CREATE TABLE summary (
    entity_meta_id   INTEGER PRIMARY KEY,
    lifetime_seconds REAL NOT NULL DEFAULT 0,
    connected_seconds REAL NOT NULL DEFAULT 0,
    lifetime_cycles  INTEGER NOT NULL DEFAULT 0,
    connection_drops INTEGER NOT NULL DEFAULT 0,
    last_state       TEXT,
    last_change_ts   INTEGER,
    updated_ts       INTEGER NOT NULL,
    reset_ts         INTEGER,                -- unix seconds of the last wear_tracker.reset; NULL = never. recompute/fold ignore pre-reset history
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);

CREATE TABLE events_fired (
    entity_meta_id  INTEGER NOT NULL,
    event_kind      TEXT NOT NULL,           -- 'wear_critical' | 'flap_anomaly' | 'connection_anomaly'
    discriminator   TEXT NOT NULL,           -- e.g. 'hours:90', 'cycles:100', 'flap', 'connection'
    fired_ts        INTEGER NOT NULL,        -- unix seconds UTC
    PRIMARY KEY (entity_meta_id, event_kind, discriminator),
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);
CREATE INDEX ix_events_fired_ts ON events_fired(fired_ts);  -- anomaly debounce (last fired within 1 h)

CREATE TABLE flap_baseline (
    entity_meta_id  INTEGER NOT NULL,
    hour_of_day     INTEGER NOT NULL,        -- 0-23
    flap_rate       REAL NOT NULL,           -- transitions/hour, mean for this hour over the last 30 days
    unavail_rate    REAL NOT NULL,           -- drops/hour, same window
    updated_ts      INTEGER NOT NULL,
    PRIMARY KEY (entity_meta_id, hour_of_day),
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE CASCADE
);

CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_meta_id  INTEGER,                 -- nullable so rows survive purge
    action          TEXT NOT NULL,           -- 'reset' | 'set_rated' | 'disable' | 'recompute' | 'purge' | 'purge_all'
    ts              INTEGER NOT NULL,
    payload         TEXT,                    -- JSON: prior/new values, actor, plus entity_id/unique_id snapshot (so purge audits stay readable after FK is SET NULL)
    FOREIGN KEY (entity_meta_id) REFERENCES entity_meta(id) ON DELETE SET NULL
);
CREATE INDEX ix_audit_log_entity_ts ON audit_log(entity_meta_id, ts);

CREATE TABLE meta (
    key TEXT PRIMARY KEY, value TEXT
);  -- last_alive, schema_version
```

**Write discipline.** `AsyncSqliteWriter` owns the SQLite connection on a single-worker thread; every read/write is a callable submitted to that thread, so all writes serialize off the event loop. A transition and its `summary` update commit together in one transaction (`write_transition_sync`). In-progress on/connected time is *not* written per event — it's folded into `summary` on the 60 s heartbeat (`apply_accruals`) and once more on shutdown; an accrual write that fails is re-queued onto the next tick rather than dropped. Shutdown is a real `EVENT_HOMEASSISTANT_STOP` async listener (`async_shutdown`) that unsubscribes every timer/subscription, awaits in-flight persist tasks, flushes pending accruals, writes a final heartbeat, and closes the connection — race-safe against a seed still running. Every commit is atomic via WAL.

**Schema migrations.** `meta.schema_version` holds the current integer version (a missing `meta` table reads as `0`). Migration scripts live at `custom_components/wear_tracker/migrations/NN_description.sql` (zero-padded ordering). On open, `AsyncSqliteWriter._apply_migrations_sync()` reads `schema_version` and applies each newer file **plus its `schema_version` stamp in one wrapping transaction**, so a crash between applying a migration and bumping the version can't leave a half-applied version that re-runs (and fails) on the next boot. Foreign-key enforcement is toggled off around the run so a table-rebuild migration can `DROP` the old parent without cascade-deleting history (surrogate ids are preserved so the restored FK constraints stay satisfied). Any failure rolls the open transaction back and closes the connection — releasing the write lock so HA's `ConfigEntryNotReady` retry surfaces the real error instead of `database is locked`. SQLite can't add a column or relax a constraint idempotently, so migrations 04 (`summary.reset_ts`) and 05 (identity rebuild) are **idempotent table rebuilds** rather than bare `ALTER`s. Downgrades are not supported — users wanting to roll back run `purge_all` and reinstall an older release.

**Retention.** `transitions` rows older than 90 days are folded into `daily_summary` then purged, hourly (`fold_and_purge_sync`). The fold is reset-aware: rows dated *on the reset day but before `summary.reset_ts`* are excluded from the fold (they are still purged) so a folded pre-reset row can't resurrect counters a reset zeroed; prior whole days still fold, as the permanent archive `keep_history` protects. `daily_summary` and `summary` are retained forever. `recompute(entity_id)` rebuilds `summary` by summing `daily_summary` (days ≥ the reset day) plus replaying remaining `transitions` with `ts ≥ reset_ts`.

**Time semantics.** Durations come from `time.monotonic()`; wall-clock `ts` comes from `dt_util.utcnow()`. Each transition row's `delta_s` records the duration of the *immediately preceding observable state period*: `(none) → first state` writes `0`; `OFF → ON` writes the off-period duration; `ON → OFF` writes the on-period duration; `OFF → DISCONNECTED` and `ON → DISCONNECTED` write the off/on-period duration that just ended; `DISCONNECTED → OFF` and `DISCONNECTED → ON` write the disconnected-period duration. Same-state transitions (`D→D`, `OFF→OFF`, `ON→ON`) write no row at all — these are the `(noop)` cells in the §4 transition table, reflecting duplicate state events from HA's bus. Connected-period totals (`summary.connected_seconds`) are *not* derived from `delta_s` — the state machine tracks a per-entity connected-interval anchor and credits the elapsed connected time on **every** transition (and on every flush), not only on disconnect. Crediting connected time in lockstep with on-time is what keeps `duty_cycle_pct = lifetime / connected` from ever exceeding 100 %. Negative monotonic deltas are clamped to 0. `daily_summary.day` is UTC; the display layer renders local at the sensor level.

**Recompute monotonicity.** `recompute` is allowed to *raise* a counter to match observed history but never to lower a value already emitted as sensor state — a `total_increasing` LTS series would otherwise show a phantom drop. If the recomputed total is lower (e.g. transient corruption inflated the prior summary), the prior value stays as a floor and a warning is logged.

**Backups.** The DB lives under `<config>/`, so HA's built-in backup integration captures `wear_tracker.db` automatically. WAL mode is per-transaction atomic, so a backup snapshot is internally consistent regardless of in-flight writes — SQLite truncates incomplete WAL frames on next open. No pre-backup hook is required. (HA's backup manager uses a callback subscription API, not a bus event; if a future version of this integration wants to checkpoint before backup, it should subscribe via `BackupManager.async_subscribe_events`, not listen on the event bus.)

**Uninstall.** Config-entry removal does *not* delete `<config>/wear_tracker/`. Reinstalling picks up where it left off (rows match by identity). For a clean slate, run `wear_tracker.purge_all` (v0.4+) or remove the directory manually.

## 6. Sensor entity classes

Each sensor entity is registered with HA's long-term statistics (LTS) and history dashboards via the right `device_class`, `state_class`, and `native_unit_of_measurement`. Each sensor's classes must be correct **on first release** — changing them after a sensor ships orphans its existing LTS rows. (v0.1 ships rows 1-5 below; v0.2 adds `wear_pct`; v0.3 adds the three rate sensors.)

| Sensor | `device_class` | `state_class` | Unit | Notes |
|---|---|---|---|---|
| `lifetime_hours` | `duration` | `total_increasing` | `h` | LTS-eligible; only decreases via `reset`. |
| `connected_hours` | `duration` | `total_increasing` | `h` | LTS-eligible. |
| `lifetime_cycles` | — | `total_increasing` | `cycles` | Integer; LTS-eligible. |
| `connection_drops` | — | `total_increasing` | `drops` | Integer; LTS-eligible. |
| `duty_cycle_pct` | — | `measurement` | `%` | Bounded [0, 100]. |
| `wear_pct` | — | `measurement` | `%` | Present when `rated_hours` **or** `rated_cycles` is set; reports the max (worst) of the two ratios. May exceed 100 once rated lifetime is reached. |
| `flap_rate_1h` | — | `measurement` | `1/h` | Updated every rollup tick. |
| `flap_rate_24h` | — | `measurement` | `1/h` | |
| `unavail_rate_1h` | — | `measurement` | `1/h` | |

`total_increasing` sensors have exactly one legitimate decrease path: the `wear_tracker.reset` service, which also clears the corresponding LTS series via `recorder.get_instance(hass).async_clear_statistics` (an internal API — the whole call is wrapped defensively so a missing/renamed recorder API just logs at DEBUG and leaves the series alone). It matches this entity's **own** sensor unique_ids exactly (not a name prefix), so resetting `switch.pump` can't wipe the statistics of a sibling like `switch.pump_2`. `recompute` enforces the never-decrease floor described in §5.

All wear sensors set `entity_category = DIAGNOSTIC` so they default off the main dashboard, and use `device_info(via_device=...)` to attach to the source entity's device.

Each sensor's unique_id is `wear_tracker_<root>_<key>`, where `root` (`registry.wear_sensor_root`) is `platform_domain_uniqueid` when the platform is known, the bare `unique_id` when the row has one but platform is still `NULL`, else the `entity_id`. Because the composite-identity work (see §4) changed this root, `async_setup_entry` runs an entity-registry migration on every boot that remaps both old root forms in place — so upgrades keep their existing sensor `entity_id`s and LTS series instead of re-registering everything as `*_2`.

## 7. Catalog format

`catalog.py`:

```python
CATALOG: dict[tuple[str, str], CatalogEntry] = {
    ("Signify Netherlands B.V.", "LWA001"): CatalogEntry(rated_hours=25000),  # Hue White
    ("Signify Netherlands B.V.", "LTA001"): CatalogEntry(rated_hours=25000),  # Hue White Ambiance
    ("LIFX",  "LIFX Mini"):       CatalogEntry(rated_hours=22500),
    ("Sengled", "*"):             CatalogEntry(rated_hours=25000),
    ("Shelly", "Shelly Plus 1"):  CatalogEntry(rated_cycles=50000),
    ("TP-Link", "*Kasa*"):        CatalogEntry(rated_cycles=40000),
}
```

`lookup_rated(manufacturer, model) -> CatalogEntry | None`:

1. **Normalize** both args: lowercase, strip whitespace, strip corporate suffixes (`b.v.`, `inc.`, `inc`, `llc`, `ltd`, `ltd.`, `gmbh`, `co.`, `co`, `corp.`, `corp`, `of sweden`, `netherlands b.v.`). So `"Signify Netherlands B.V."`, `"signify"`, and `"Signify"` all collapse to `"signify"`.
2. If `manufacturer` is empty after normalization → return `None`. The catalog is not used for entities without identifiable hardware; `wear_pct` is then `unknown` rather than a misleading guess.
3. Try exact normalized `(manufacturer, model)`.
4. Try `(manufacturer, glob)` where `glob` is any catalog model containing `*` and glob-matches the normalized model.
5. Try `(manufacturer, "*")` as a manufacturer-wide default.
6. Log the first miss per `(manufacturer, model)` pair at `DEBUG` so users can contribute entries.

Catalog is data-only and PR-able; v1.0 may move it to YAML under `custom_components/wear_tracker/catalog/` so contributors don't need code review.

## 8. Discovery & config flow

### v0.1 (YAML only)

```yaml
wear_tracker:
  entities:
    - light.kitchen_island
    - switch.fish_tank
```

(`auto_discovery_mode` is v0.2+; no discovery exists in v0.1.)

### v0.2 (UI install) — first-install wizard

1. User clicks "Add Integration → Wear Tracker."
2. Wizard scans `entity_registry` for all entities in trackable domains.
3. Step `bulk_confirm`: shows one checkbox group per domain:
   - `Track all 19 lights? [x]`
   - `Track all 29 switches? [x]`
   - `Track all 4 fans? [x]`
   - ... `binary_sensor` is unchecked by default and labeled "opt-in (noisy)".
4. Step `discovery_mode`: radio — `prompt` (default) / `auto_track` / `off`.
5. On submit: writes `entity_meta` rows, looks up each via `catalog.lookup_rated`, creates sensor entities.

### New-device prompts (v0.2+)

- Coordinator listens to `event_entity_registry_updated` (action=`create`).
- The create handler first skips the integration's **own** entities (`platform == wear_tracker`, e.g. the health-alert binary sensors — tracking them would loop discovery), already-excluded entities/domains, and non-trackable domains. A `create` for a previously-removed `entity_id` still present in the tracked options instead schedules a reload, resuming via the §4 identity path.
- If entity domain is trackable and `discovery_mode == "prompt"`, create a Repair Issue via `ir.async_create_issue` with `severity=warning`, `is_fixable=True`, `translation_key="new_device"`, and `data={"entity_id": ..., "domain": ...}`.
- The Repair fix flow (`repairs.py:NewDeviceRepairFlow`) presents three menu options: **Track**, **Skip**, **Never ask for this domain**.
- "Never" updates option `excluded_domains` on the config entry.
- `auto_track` mode skips the issue and adds the entity to the tracked options directly.

### Options flow

Currently exposes two settings: the discovery mode (`prompt` / `auto_track` / `off`) and the `include_binary_sensors` toggle. Per-entity `rated_hours` / `rated_cycles` and `disabled` are changed through the `set_rated` / `disable` services (§9), not the options flow.

## 9. Service catalog (`services.yaml`)

| Service | Arguments | Behavior | Returns |
|---|---|---|---|
| `wear_tracker.reset` | `entity_id: str`, `keep_history: bool = false` | Zeroes `summary` for entity (recording `reset_ts` so `recompute`/fold won't resurrect pre-reset counters) and clears its LTS series. If `keep_history=false`, also deletes its `transitions` and `daily_summary`. Clears only the `wear_critical` debounce rows (re-arming wear alerts for a replaced device) while leaving flap/connection anomaly debounce intact. Records prior counter values to `audit_log`, then reloads the entry. | none |
| `wear_tracker.set_rated` | `entity_id: str`, `hours: float?`, `cycles: int?` | Updates `entity_meta.rated_hours` / `rated_cycles` (at least one required). Reloads the entry so sensors/`wear_pct` pick up the new rating. | none |
| `wear_tracker.export_log` | `entity_id: str`, `start: datetime`, `end: datetime`, `filename: str?` | Writes CSV (`ts,from,to,raw_from,raw_to,delta_s`) to `<config>/wear_tracker/exports/<filename>` (default: `<entity>_<start>.csv`). `filename` is rejected if it contains path separators, `..`, a leading `.`, or doesn't end in `.csv`; the resolved path must stay under the exports directory. | `{path: str, rows: int}` via response data. |
| `wear_tracker.recompute` | `entity_id: str?` (default: all) | Rebuilds `summary` from `daily_summary` + remaining `transitions` (at/after `reset_ts`, never lowering a counter). Dispatches a sensor refresh; no reload. | `{recomputed: [entity_id, ...]}` |
| `wear_tracker.disable` | `entity_id: str`, `disabled: bool = true` | Sets `entity_meta.disabled` with `disabled_reason='user'` (sticky across restarts, unlike a registry-removal disable). Stops accruing but keeps history. Reloads the entry. | none |
| `wear_tracker.purge` | `entity_id: str` | Hard-deletes the `entity_meta` row; cascades `transitions`, `daily_summary`, `summary`, `events_fired`, `flap_baseline`. Also drops the entity from the entry's tracked options (and, in `auto_track`, adds it to `excluded_entities`) so the reload can't re-create a zeroed row. Audit row written *before* delete with `entity_id`/`unique_id` in `payload`. Awaits one reload (the options-update listener's duplicate reload is suppressed). Irreversible. | none |
| `wear_tracker.purge_all` | `confirm: bool` (must be `true`) | Hard-deletes every `entity_meta` row and its cascades (keeps `audit_log`). Clears the tracked options (and in `auto_track` excludes every tracked entity) so the awaited reload can't re-upsert zeroed rows or re-discover still-present entities. Skipped entirely unless `confirm=true`; this guards against accidental invocation from automations. | `{purged: int}` |

All services use `async_register_admin_service` (admin-only) and standard voluptuous schemas. Every admin-action service (everything except `export_log`) writes a row to `audit_log` with prior/new values in `payload` (JSON), so the `audit_log.action` enum (`reset`, `set_rated`, `disable`, `recompute`, `purge`, `purge_all`) is exhaustive.

## 10. Event spec

| Event | Payload | Fired when |
|---|---|---|
| `wear_tracker.wear_critical` | `{entity_id, metric: "hours"\|"cycles", pct: 90\|95\|100, value, rated}` | First crossing of each threshold. `rated_hours` and `rated_cycles` are evaluated independently; each metric+threshold pair (`hours:90`, `cycles:100`, …) debounces separately via its own `events_fired` row, effectively once (debounce ≈ ∞). |
| `wear_tracker.flap_anomaly` | `{entity_id, flap_rate_1h, baseline_30d, ratio}` | `flap_rate_1h > 5 * baseline_30d` and `flap_rate_1h >= 6` transitions/hour. Min 1 h between repeats per entity. |
| `wear_tracker.connection_anomaly` | `{entity_id, unavail_rate_1h, baseline_30d, ratio}` | `unavail_rate_1h > 5 * baseline_30d` and `>= 3` drops/hour. Min 1 h between repeats. |

Baseline = mean of the same hour-of-day across the last 30 days of `transitions` (flap = all transitions; unavail = `→ DISCONNECTED` only), with a floor of 0.2 to avoid div-by-zero amplification on quiet devices. Per-entity, per-hour-of-day means live in the `flap_baseline` table, populated lazily and refreshed for the current hour when the cached row is older than one hour (`BASELINE_REFRESH_S`); there is no separate nightly recompute. The rollup tick reads (and lazily refreshes) that cache rather than re-scanning `transitions` on every tick.

## 11. Phase-by-phase delivery

Each phase is a discrete *milestone*, not a single coding session — most phases span multiple sessions. Phase boundaries are chosen for shippable user value (v0.1 = persistence works, v0.2 = no YAML needed, v0.3 = anomaly warnings fire, v0.4 = automation/admin hooks, v1.0 = HACS-listed).

### v0.1 — MVP

Files to create: `manifest.json`, `hacs.json`, `const.py`, `__init__.py`, `coordinator.py`, `state_machine.py`, `storage.py` (writer + migration runner), `migrations/01_initial.sql` (creates `entity_meta` / `transitions` / `daily_summary` / `summary` / `meta` + their indexes), `registry.py`, `sensor.py`, voluptuous YAML schema in `__init__.py`, `README.md`.

Sensors exposed per tracked entity: `<name>_connected_hours`, `<name>_lifetime_hours`, `<name>_lifetime_cycles`, `<name>_connection_drops`, `<name>_duty_cycle_pct`.

Tests: `test_state_machine.py` (transition table, edge cases), `test_storage.py` (schema, atomic write, recompute), `test_coordinator.py` (mock state_changed events).

Manual verification: install via custom_components symlink, configure 2 lights + 1 switch in YAML, toggle them, restart HA, confirm counters survive.

### v0.2 — Config flow + discovery + catalog

Add: `config_flow.py` with `user`, `bulk_confirm`, `discovery_mode` steps; `discovery.py`; `repairs.py`; `catalog.py` with seed entries; `strings.json` + `translations/en.json`; add `wear_pct` sensor.

Tests: `test_config_flow.py`, `test_discovery.py`, `test_catalog.py`.

Manual: fresh install in dev HA, walk wizard, plug in a new fake light, confirm Repair Issue appears, resolve all three branches.

### v0.3 — Flap rate + health

Add: `migrations/02_flap_and_events.sql` (creates `flap_baseline` and `events_fired` + indexes), `rollup.py` with `compute_flap_rate(window_s)` + lazy `flap_baseline` population, `flap_rate_1h` / `flap_rate_24h` / `unavail_rate_1h` sensors, `binary_sensor.py:HealthAlertBinarySensor`, `events.py` for `flap_anomaly` + `connection_anomaly`.

Tests: `test_rollup.py` with synthetic transition streams, threshold edge cases, debounce of repeat events.

Manual: induce flapping via a `script` that toggles a fake switch 20× in 5 min; confirm `health_alert` fires.

### v0.4 — Services + daily rollup

Add: `migrations/03_audit_log.sql` (creates `audit_log` + index), `services.py` (all admin services including `purge` and `purge_all`), `services.yaml`, hourly cron via `async_track_time_interval` for the `transitions → daily_summary` fold + 90 d purge, `wear_critical` event at 90/95/100%. (`flap_baseline` refreshes lazily on the rollup tick, not via a nightly cron. Post-v0.4 correctness fixes added `migrations/04_summary_reset_ts.sql` and `migrations/05_platform_and_disabled_reason.sql` — see §4/§5.)

Tests: `test_services.py` (reset/set_rated/export_log/recompute/disable round-trips), retention/fold idempotence test.

Manual: export CSV for an entity, eyeball it; run `recompute`, confirm `summary` matches pre-recompute values within float tolerance.

### v1.0 — Polish + HACS submission

Add: full English translations, `diagnostics.py`, `.github/workflows/ci.yml` (pytest + ruff + mypy), `hassfest.yml`, `release.yml` (auto-tag + changelog), expanded catalog (community PRs welcomed), `CONTRIBUTING.md`, screenshots in README, brand icon submitted to `home-assistant/brands`.

Manual: install on a fresh HA OS instance via HACS custom repo, follow README from scratch, validate every screenshot.

## 12. HACS submission checklist

- [ ] `manifest.json` has `domain`, `name`, `documentation`, `issue_tracker`, `codeowners`, `requirements`, `version`, `integration_type=service`, `iot_class=calculated`, `config_flow=true`.
- [ ] `hacs.json` with `name`, `content_in_root=false`, `render_readme=true`, `zip_release=false`, `homeassistant` minimum version pinned.
- [ ] Repo topics include `home-assistant`, `hacs`, `home-assistant-custom`.
- [ ] README has install instructions (HACS + manual), screenshots, configuration table, FAQ.
- [ ] Brand icon merged into `home-assistant/brands` (`custom_integrations/wear_tracker/`).
- [ ] GitHub Action runs `hassfest` and HACS Action (`hacs/action` pinned to a tagged release — not `@main`, which can break unexpectedly — with `category: integration`) on every push and passes.
- [ ] Tagged release (semver) with release notes.
- [ ] PR opened against `hacs/default` listing the repo under `integration`.
- [ ] Issue/PR templates in `.github/ISSUE_TEMPLATE/`.

## 13. Open questions / decisions deferred

- **Multi-entity composite devices.** A Shelly relay with energy attributes is one device but several entities; should cycles be deduplicated per device? Defer to v0.3 user feedback.
- **Energy correlation.** Surfacing kWh/hour alongside on-hours would make wear data more useful for cost analysis. Out of scope for v1.0; consider as v1.1.
- **Manual cycle correction.** Should there be a `wear_tracker.adjust(entity_id, hours_delta, cycles_delta)` for users who replaced a bulb without resetting? Probably yes in v0.4; sketched but not committed.
- **Per-entity debounce tuning UX.** Currently options-flow only; consider a Repair Issue when an entity's flap rate is dominated by sub-debounce noise suggesting the user raise `debounce_s`.
- **iOS/Android UI cards.** Lovelace card (separate repo `wear-tracker-card`) for at-a-glance bulb wear gauge — post-1.0.
