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
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
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
        raise ConfigEntryNotReady(f"could not open {db_path}") from err

    coordinator = WearCoordinator(hass, writer, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    services.async_register_services(hass)

    entity_ids = discovery.resolve_tracked_entities(hass, dict(entry.options))
    try:
        # Source states may not be loaded yet at boot; the state-change
        # subscription picks them up, so we don't gate on async_at_started.
        await coordinator.async_start(entity_ids)
    except Exception as err:
        _LOG.exception("wear_tracker: coordinator failed to start")
        await coordinator.async_shutdown()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise ConfigEntryNotReady("coordinator failed to start") from err
    _LOG.info("wear_tracker: tracking %d entities", len(entity_ids))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, lambda _evt: coordinator.async_shutdown()
        )
    )
    return True


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
    await hass.config_entries.async_reload(entry.entry_id)
