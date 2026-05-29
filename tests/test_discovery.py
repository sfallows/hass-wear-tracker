"""New-device discovery: auto_track, prompt (Repair issue), and catalog->wear_pct."""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

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
