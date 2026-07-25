# hass-wear-tracker

A Home Assistant custom integration that tracks **lifetime usage** for any entity — so you can finally answer "is this bulb still good?" or "did this relay hit its rated cycles?"

> **Status:** v0.1-v0.4 implemented — core tracking, config-flow UI + discovery, anomaly detection, and admin services. Not yet listed in the HACS default repository. See [`DESIGN.md`](./DESIGN.md) for the full spec.

## Why this exists

Smart bulbs, relays, HVAC, vacuums — most have a manufacturer-rated lifetime (Hue White = 25,000 hours, Shelly relay = 50,000 cycles). Home Assistant's `recorder` purges history after ~10 days, `history_stats` is limited, and no existing HACS integration tracks cumulative usage **independent of recorder retention**.

This integration:

- Maintains a **separate, durable SQLite log** under `<config>/wear_tracker/wear_tracker.db` — survives recorder purges, HA restarts, and crashes.
- Tracks both **`connected_hours`** (time the entity was reachable) and **`lifetime_hours`** (subset of that time the entity was in its "on" state) — two different wear signals.
- Counts **`lifetime_cycles`** (off→on transitions) and **`connection_drops`** (available→unavailable transitions).
- Exposes **`wear_pct`** comparing accumulated hours to a rated lifetime from a built-in catalog of common devices (or your own override).
- Detects **flap-rate anomalies** — if an entity's hourly transition rate is 5× its 30-day baseline, fires `wear_tracker.flap_anomaly` so you can route an alert to Telegram, Pushover, or push notifications.
- Fires **`wear_tracker.wear_critical`** at 90/95/100 % of rated hours **or** cycles, and **`wear_tracker.connection_anomaly`** when an entity's unavailable rate spikes above its baseline — both routable to notifications.
- Provides **full audit export** — service `wear_tracker.export_log(entity_id, start, end)` writes a CSV of every raw state transition.

## Supported entity types

Default trackable domains (configurable):

- `light` · `switch` · `fan`
- `climate` (HVAC)
- `water_heater` · `cover` · `vacuum`
- `media_player` (projector lamp hours, TV runtime)

`binary_sensor` is supported but opt-in (most binary sensors aren't wear-tracking candidates).

## Installation

### HACS (planned, not yet listed)

Once published to the HACS default repository:

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/sfallows/hass-wear-tracker` as Integration
2. Install **Wear Tracker**
3. Restart Home Assistant
4. Settings → Devices & Services → Add Integration → **Wear Tracker**

### Manual

```bash
cd <config>/custom_components/
git clone https://github.com/sfallows/hass-wear-tracker wear_tracker
# Restart HA
```

## First-time setup

When you add the integration, a wizard scans your entity registry and asks one bulk-confirm question per domain:

```
Track all 19 lights? [x]
Track all 29 switches? [x]
Track all 4 fans? [x]
Track all 2 vacuums? [x]
... binary_sensor unchecked by default
```

Then pick a discovery mode for future new devices:

- **`prompt` (default)** — when a new trackable entity appears, HA shows a Repair Issue ("Track `light.foo`?" Yes / Skip / Never)
- **`auto_track`** — silently start tracking every new entity in trackable domains, using the catalog's default rated hours
- **`off`** — manual additions only

## Sensors created per tracked entity

| Sensor | What it measures |
|---|---|
| `sensor.<name>_lifetime_hours` | Cumulative time the entity was in its "on" state |
| `sensor.<name>_connected_hours` | Cumulative time the entity was reachable (not `unavailable`) |
| `sensor.<name>_lifetime_cycles` | Cumulative on-cycles (off→on, and recovered-from-unavailable→on; sub-2 s bounces excluded by default) |
| `sensor.<name>_connection_drops` | Total available→unavailable transitions |
| `sensor.<name>_duty_cycle_pct` | `lifetime_hours / connected_hours × 100` |
| `sensor.<name>_wear_pct` | Worst of `lifetime_hours / rated_hours` and `lifetime_cycles / rated_cycles`, ×100 (if a rating is set) |
| `sensor.<name>_flap_rate_1h` | Transitions in the last hour |
| `sensor.<name>_flap_rate_24h` | Transitions per hour, averaged over the last 24 h |
| `sensor.<name>_unavail_rate_1h` | Connection drops in the last hour |
| `binary_sensor.<name>_health_alert` | True when flap rate is anomalously high |

## Domain-specific notes

Different domains wear differently, so the integration interprets state per-domain:

- **Climate** — On-time keys off `attributes.hvac_action` (actively heating/cooling/drying/fan/preheating/defrosting), not the thermostat mode. A thermostat left in `heat` mode all winter will still read zero on-time on mild days when the compressor is idle.
- **Cover** — On-time counts only when the motor is moving (`opening`/`closing`); sitting `open` doesn't accrue hours. For covers, `lifetime_cycles` is the wear signal worth watching.
- **Water heater** — Home Assistant has no `hvac_action` equivalent for water heaters, so this integration counts "energized hours" (mode != `off`). Pair with a power sensor if you want true active-heating time.
- **Media player** — `playing` and `buffering` count as on; brief buffering blips are filtered out by the 2 s debounce.

## Roadmap

- **v0.1 (MVP)** — core lifetime tracking, YAML config, SQLite persistence, sensors
- **v0.2** — config flow UI, Repair Issue discovery, catalog of common devices
- **v0.3** — flap-rate anomaly detection, health alerts
- **v0.4** — services (reset, export_log, recompute), daily summary rollup
- **v1.0** — full catalog, translations, tests, HACS default-repo submission

See [`DESIGN.md`](./DESIGN.md) for the full spec.

## Contributing

Catalog PRs especially welcome — if you have a device with a known rated lifetime, add it to `catalog.py`. See `DESIGN.md` § Catalog format.

## License

MIT
