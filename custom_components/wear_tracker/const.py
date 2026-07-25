"""Constants for the wear_tracker integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "wear_tracker"
PLATFORMS: Final = ["sensor", "binary_sensor"]

DB_SUBDIR: Final = "wear_tracker"
DB_FILENAME: Final = "wear_tracker.db"

CONF_ENTITIES: Final = "entities"
CONF_AUTO_TRACK: Final = "auto_track"
CONF_INCLUDE_BINARY_SENSORS: Final = "include_binary_sensors"
CONF_EXCLUDE: Final = "exclude"
CONF_DISCOVERY_MODE: Final = "discovery_mode"
CONF_EXCLUDED_DOMAINS: Final = "excluded_domains"
CONF_EXCLUDED_ENTITIES: Final = "excluded_entities"

DISCOVERY_PROMPT: Final = "prompt"
DISCOVERY_AUTO_TRACK: Final = "auto_track"
DISCOVERY_OFF: Final = "off"
DISCOVERY_MODES: Final = (DISCOVERY_PROMPT, DISCOVERY_AUTO_TRACK, DISCOVERY_OFF)

SIGNAL_WEAR_UPDATED: Final = f"{DOMAIN}_wear_updated"

# hass.data key holding a set of entry_ids whose next options-update-driven reload
# should be suppressed (purge/purge_all run their own awaited reload, so the update
# listener's duplicate reload is skipped once). Kept separate from hass.data[DOMAIN]
# (the coordinator map) so it never confuses coordinator iteration.
RELOAD_SUPPRESS: Final = f"{DOMAIN}_reload_suppress"

EVENT_FLAP_ANOMALY: Final = f"{DOMAIN}.flap_anomaly"
EVENT_CONNECTION_ANOMALY: Final = f"{DOMAIN}.connection_anomaly"
EVENT_WEAR_CRITICAL: Final = f"{DOMAIN}.wear_critical"

DEFAULT_DEBOUNCE_S: Final = 2.0
HEARTBEAT_INTERVAL_S: Final = 60
ROLLUP_INTERVAL_S: Final = 60

# Flap / connection anomaly detection (DESIGN §10).
FLAP_RATIO: Final = 5.0
FLAP_MIN_PER_HOUR: Final = 6.0
CONNECTION_RATIO: Final = 5.0
CONNECTION_MIN_PER_HOUR: Final = 3.0
BASELINE_FLOOR: Final = 0.2
BASELINE_WINDOW_DAYS: Final = 30
BASELINE_REFRESH_S: Final = 3600
ANOMALY_DEBOUNCE_S: Final = 3600

# v0.4 retention + wear-critical (DESIGN §5, §10).
RETENTION_DAYS: Final = 90
RETENTION_INTERVAL_S: Final = 3600
WEAR_CRITICAL_THRESHOLDS: Final = (90, 95, 100)
WEAR_CRITICAL_DEBOUNCE_S: Final = 10**12  # effectively once per threshold

# On restart, credit an in-progress ON period across the downtime gap only if the
# gap since the last heartbeat is within this window (one missed beat tolerated).
RESTART_GAP_TOLERANCE_S: Final = 2 * HEARTBEAT_INTERVAL_S

# Hard cap on the seconds credited for a recovered restart on-period. A slow boot
# or seed-retry backoff could otherwise fabricate many minutes of on-time if the
# device was toggled externally during the unobserved window.
RESTART_CREDIT_MAX_S: Final = 300

# Deferred seeding retries itself on a transient boot-time DB error (previously a
# ConfigEntryNotReady let HA retry with backoff); capped exponential backoff.
SEED_RETRY_INITIAL_S: Final = 30
SEED_RETRY_MAX_S: Final = 300

TRACKABLE_DOMAINS: Final = frozenset(
    {
        "light",
        "switch",
        "fan",
        "climate",
        "water_heater",
        "cover",
        "vacuum",
        "media_player",
        "binary_sensor",
    }
)
