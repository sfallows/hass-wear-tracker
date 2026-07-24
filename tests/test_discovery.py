"""New-device discovery: auto_track, prompt (Repair issue), and catalog->wear_pct."""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.wear_tracker import discovery  # noqa: E402

DOMAIN = "wear_tracker"


async def _setup_entry(hass, options):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_auto_track_picks_up_new_entity(hass, enable_custom_integrations):
    entry = await _setup_entry(hass, {"entities": [], "discovery_mode": "auto_track"})

    er.async_get(hass).async_get_or_create(
        "switch", "demo", "uid-new", suggested_object_id="pump"
    )
    await hass.async_block_till_done()

    # async_add_tracked_entities updated options and reloaded the entry.
    assert "switch.pump" in entry.options["entities"]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert "switch.pump" in coordinator.tracked_entity_ids()


async def test_prompt_mode_raises_repair_issue(hass, enable_custom_integrations):
    await _setup_entry(hass, {"entities": [], "discovery_mode": "prompt"})

    er.async_get(hass).async_get_or_create(
        "switch", "demo", "uid-new", suggested_object_id="pump"
    )
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "new_device_switch.pump")
    assert issue is not None
    assert issue.data == {"entity_id": "switch.pump", "domain": "switch"}


async def test_off_mode_ignores_new_entity(hass, enable_custom_integrations):
    entry = await _setup_entry(hass, {"entities": [], "discovery_mode": "off"})
    er.async_get(hass).async_get_or_create(
        "switch", "demo", "uid-new", suggested_object_id="pump"
    )
    await hass.async_block_till_done()
    assert entry.options["entities"] == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, "new_device_switch.pump") is None


async def test_catalog_populates_rated_and_wear_pct_sensor(hass, enable_custom_integrations):
    source = MockConfigEntry(domain="demo")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("demo", "bulb-1")},
        manufacturer="Signify Netherlands B.V.",
        model="LWA001",
    )
    er.async_get(hass).async_get_or_create(
        "light", "demo", "uid-bulb", device_id=device.id, suggested_object_id="bulb"
    )
    hass.states.async_set("light.bulb", "on")

    entry = await _setup_entry(hass, {"entities": ["light.bulb"], "discovery_mode": "off"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.get_tracked("light.bulb").rated_hours == 25000

    wear_pct = [
        e
        for e in er.async_get(hass).entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith("_wear_pct")
    ]
    assert len(wear_pct) == 1


async def test_cycles_only_device_gets_wear_sensor(hass, enable_custom_integrations):
    """A catalog device rated only in cycles (e.g. a Shelly relay) must still get a
    wear_pct sensor (sensor.py finding)."""
    source = MockConfigEntry(domain="demo")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("demo", "relay-1")},
        manufacturer="Shelly",
        model="Shelly Plus 1",
    )
    er.async_get(hass).async_get_or_create(
        "switch", "demo", "uid-relay", device_id=device.id, suggested_object_id="relay"
    )
    hass.states.async_set("switch.relay", "on")

    entry = await _setup_entry(hass, {"entities": ["switch.relay"], "discovery_mode": "off"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    tracked = coordinator.get_tracked("switch.relay")
    assert tracked.rated_cycles == 50000
    assert tracked.rated_hours is None

    wear_pct = [
        e
        for e in er.async_get(hass).entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith("_wear_pct")
    ]
    assert len(wear_pct) == 1


async def test_scan_trackable_skips_own_entities(hass, enable_custom_integrations):
    """Finding 3: our own health-alert binary_sensor must never be discovered."""
    hass.states.async_set("light.kitchen", "on")
    entry = await _setup_entry(
        hass,
        {"entities": ["light.kitchen"], "discovery_mode": "off", "include_binary_sensors": True},
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    health = next(
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith("_health_alert")
    )
    found = discovery.scan_trackable(hass, include_binary_sensors=True)
    assert health not in found
    assert health not in coordinator.tracked_entity_ids()


async def test_auto_track_ignores_own_health_alert(hass, enable_custom_integrations):
    """Finding 3: auto_track + include_binary_sensors must not track our own
    health-alert sensor (which would loop into unbounded reloads)."""
    er.async_get(hass).async_get_or_create(
        "light", "demo", "uid-kitchen", suggested_object_id="kitchen"
    )
    hass.states.async_set("light.kitchen", "on")
    entry = await _setup_entry(
        hass,
        {"entities": [], "discovery_mode": "auto_track", "include_binary_sensors": True},
    )
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]

    tracked = coordinator.tracked_entity_ids()
    assert not any(t.endswith("_health_alert") for t in tracked)
    health = [
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith("_health_alert")
    ]
    assert all(h not in tracked for h in health)


async def test_excluded_entity_not_auto_tracked(hass, enable_custom_integrations):
    """Finding 4: an explicitly excluded entity must not be auto-tracked when its
    registry entry (re)appears."""
    entry = await _setup_entry(
        hass,
        {
            "entities": [],
            "discovery_mode": "auto_track",
            "excluded_entities": ["switch.pump"],
        },
    )
    er.async_get(hass).async_get_or_create(
        "switch", "demo", "uid-new", suggested_object_id="pump"
    )
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert "switch.pump" not in coordinator.tracked_entity_ids()
    assert "switch.pump" not in entry.options["entities"]


def test_resolve_filters_explicit_entries_against_excluded(hass):
    """Finding 4: exclusion wins over a stale explicit CONF_ENTITIES membership."""
    resolved = discovery.resolve_tracked_entities(
        hass,
        {
            "entities": ["light.a", "light.b"],
            "excluded_entities": ["light.b"],
            "discovery_mode": "off",
        },
    )
    assert resolved == ["light.a"]
