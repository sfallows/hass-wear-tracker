"""Entity discovery helpers (DESIGN §8).

Scans the entity registry for entities in trackable domains, for the config-flow
bulk-confirm wizard and for `auto_track` mode.
"""
from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_DISCOVERY_MODE,
    CONF_ENTITIES,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITIES,
    CONF_INCLUDE_BINARY_SENSORS,
    DISCOVERY_AUTO_TRACK,
    TRACKABLE_DOMAINS,
)


@callback
def scan_trackable(
    hass: HomeAssistant,
    *,
    include_binary_sensors: bool = False,
    excluded_domains: Iterable[str] = (),
    excluded_entities: Iterable[str] = (),
) -> list[str]:
    """Return enabled entity_ids in trackable domains, honoring exclusions."""
    excluded_domains = set(excluded_domains)
    excluded_entities = set(excluded_entities)
    ent_reg = er.async_get(hass)
    found: list[str] = []
    for entry in ent_reg.entities.values():
        if entry.disabled:
            continue
        domain = entry.domain
        if domain not in TRACKABLE_DOMAINS:
            continue
        if domain == "binary_sensor" and not include_binary_sensors:
            continue
        if domain in excluded_domains or entry.entity_id in excluded_entities:
            continue
        found.append(entry.entity_id)
    return found


@callback
def trackable_by_domain(hass: HomeAssistant) -> dict[str, list[str]]:
    """Group all trackable entities (incl. binary_sensor) by domain for the wizard."""
    grouped: dict[str, list[str]] = {}
    for entity_id in scan_trackable(hass, include_binary_sensors=True):
        grouped.setdefault(entity_id.split(".", 1)[0], []).append(entity_id)
    return grouped


@callback
def resolve_tracked_entities(hass: HomeAssistant, options: dict) -> list[str]:
    """Compute the entity_ids to track from merged entry data/options."""
    explicit = list(options.get(CONF_ENTITIES, []))
    if options.get(CONF_DISCOVERY_MODE) == DISCOVERY_AUTO_TRACK:
        explicit.extend(
            scan_trackable(
                hass,
                include_binary_sensors=options.get(CONF_INCLUDE_BINARY_SENSORS, False),
                excluded_domains=options.get(CONF_EXCLUDED_DOMAINS, []),
                excluded_entities=set(options.get(CONF_EXCLUDED_ENTITIES, [])) | set(explicit),
            )
        )
    seen: set[str] = set()
    ordered: list[str] = []
    for entity_id in explicit:
        if entity_id not in seen:
            seen.add(entity_id)
            ordered.append(entity_id)
    return ordered
