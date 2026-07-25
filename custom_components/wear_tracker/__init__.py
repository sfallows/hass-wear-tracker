"""hass-wear-tracker — entry point.

v0.2: UI config entry (see config_flow.py). A v0.1-style YAML block is still
accepted and imported into a config entry:

```yaml
wear_tracker:
  entities:
    - light.kitchen_island
    - switch.fish_tank
  auto_track: true            # -> discovery_mode: auto_track
  include_binary_sensors: false
  exclude:
    - light.living_lamp
```
"""
from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from . import discovery, services
from .const import (
    CONF_AUTO_TRACK,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    CONF_INCLUDE_BINARY_SENSORS,
    DB_FILENAME,
    DB_SUBDIR,
    DOMAIN,
    PLATFORMS,
    RELOAD_SUPPRESS,
)
from .coordinator import WearCoordinator
from .storage import AsyncSqliteWriter

_LOG = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            lambda value: value or {},
            vol.Schema(
                {
                    vol.Optional(CONF_ENTITIES, default=list): vol.All(
                        cv.ensure_list, [cv.entity_id]
                    ),
                    vol.Optional(CONF_AUTO_TRACK, default=False): cv.boolean,
                    vol.Optional(CONF_INCLUDE_BINARY_SENSORS, default=False): cv.boolean,
                    vol.Optional(CONF_EXCLUDE, default=list): vol.All(
                        cv.ensure_list, [cv.entity_id]
                    ),
                }
            ),
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Import a YAML block into a config entry, if present."""
    domain_config = config.get(DOMAIN)
    if domain_config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=domain_config
            )
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    db_path = Path(hass.config.path(DB_SUBDIR)) / DB_FILENAME
    writer = AsyncSqliteWriter(db_path)
    try:
        await writer.open()
    except Exception as err:
        _LOG.exception("wear_tracker: failed to open DB at %s", db_path)
        # Close the writer so its executor (and any connection the open half-created)
        # is released; otherwise the ConfigEntryNotReady retry below would race a
        # leaked write lock and fail with 'database is locked' not the real error.
        await writer.close()
        raise ConfigEntryNotReady(f"could not open {db_path}") from err

    coordinator = WearCoordinator(hass, writer, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    services.async_register_services(hass)

    entity_ids = discovery.resolve_tracked_entities(hass, dict(entry.options))
    try:
        # Machine seeding is gated on HA-started inside the coordinator so it
        # observes real source states rather than a spurious DISCONNECTED.
        await coordinator.async_start(entity_ids)
    except Exception as err:
        _LOG.exception("wear_tracker: coordinator failed to start")
        await coordinator.async_shutdown()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise ConfigEntryNotReady("coordinator failed to start") from err
    _LOG.info("wear_tracker: tracking %d entities", len(entity_ids))

    async def _async_on_ha_stop(_event: Event) -> None:
        await coordinator.async_shutdown()

    await _async_migrate_sensor_unique_ids(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_ha_stop)
    )
    return True


async def _async_migrate_sensor_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: WearCoordinator
) -> None:
    """Rewrite pre-composite sensor/binary_sensor unique_ids to the new
    platform-qualified roots in the HA entity registry (DESIGN §4). The old root
    was the bare unique_id; `wear_sensor_root` now qualifies it with
    platform+domain. Without this migration an upgrade would register every
    sensor anew (as *_2), strand the originals' history/LTS, and break dashboards
    and automations keyed on the old entity_ids."""
    from .registry import wear_sensor_root
    from .sensor import WearPercent, _SENSOR_CLASSES

    keys = {cls.SENSOR_KEY for cls in _SENSOR_CLASSES}
    keys |= {WearPercent.SENSOR_KEY, "health_alert"}

    tracked = [
        t
        for t in (coordinator.get_tracked(eid) for eid in coordinator.tracked_entity_ids())
        if t is not None
    ]

    def _old_roots(t) -> set[str]:
        # Each entity may have been registered under either pre-composite root: the
        # bare unique_id (rows that already had a unique_id) or the entity_id (rows
        # with no unique_id pre-upgrade that got one backfilled this same boot).
        roots = {t.entity_id}
        if t.unique_id:
            roots.add(t.unique_id)
        return roots

    # A root shared by two tracked entities was the very collision the new scheme
    # fixes; it can't be attributed to either, so leave it alone.
    old_root_counts: dict[str, int] = {}
    for t in tracked:
        for old_root in _old_roots(t):
            old_root_counts[old_root] = old_root_counts.get(old_root, 0) + 1

    remap: dict[str, str] = {}
    for t in tracked:
        new_root = wear_sensor_root(t)
        for old_root in _old_roots(t):
            if old_root == new_root or old_root_counts[old_root] > 1:
                continue
            for key in keys:
                remap[f"wear_tracker_{old_root}_{key}"] = f"wear_tracker_{new_root}_{key}"
    if not remap:
        return

    ent_reg = er.async_get(hass)

    @callback
    def _migrate(reg_entry: er.RegistryEntry) -> dict[str, str] | None:
        new_uid = remap.get(reg_entry.unique_id)
        if new_uid is None:
            return None
        # Don't rewrite onto a unique_id already taken (a partial prior migration,
        # or the shared-root collision edge) — async_update_entity would raise.
        if ent_reg.async_get_entity_id(reg_entry.domain, DOMAIN, new_uid):
            return None
        return {"new_unique_id": new_uid}

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: WearCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if coordinator is not None:
        await coordinator.async_shutdown()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        services.async_unregister_services(hass)
    return unloaded


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # purge/purge_all update the options to drop the purged entities, then run their
    # own awaited reload. Skip this listener's duplicate reload for that one update
    # so a purge costs a single setup cycle. The token is one-shot (discarded here).
    suppress = hass.data.get(RELOAD_SUPPRESS)
    if suppress is not None and entry.entry_id in suppress:
        suppress.discard(entry.entry_id)
        return
    await hass.config_entries.async_reload(entry.entry_id)
