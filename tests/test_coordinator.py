"""Coordinator + config-entry setup through real Home Assistant.

Requires the HA test harness (pytest-homeassistant-custom-component); skipped
otherwise. Accrual/rename/unique_id *logic* is covered without HA in
test_state_machine.py and test_storage.py.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import EntityCategory  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_capture_events,
)

DOMAIN = "wear_tracker"


async def _setup_entry(hass, options):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]


async def _register_light(hass, object_id, unique_id):
    er.async_get(hass).async_get_or_create(
        "light", "demo", unique_id, suggested_object_id=object_id
    )
    hass.states.async_set(f"light.{object_id}", "on")


async def test_setup_entry_with_no_entities(hass, enable_custom_integrations):
    _entry, coordinator = await _setup_entry(
        hass, {"entities": [], "discovery_mode": "off"}
    )
    assert coordinator.tracked_entity_ids() == []


async def test_creates_sensors_per_entity(hass, enable_custom_integrations):
    hass.states.async_set("light.kitchen", "on")
    await _setup_entry(hass, {"entities": ["light.kitchen"], "discovery_mode": "off"})

    ent_reg = er.async_get(hass)
    sensors = [
        e
        for e in ent_reg.entities.values()
        if e.platform == DOMAIN and e.domain == "sensor"
    ]
    assert all(e.entity_category == EntityCategory.DIAGNOSTIC for e in sensors)
    suffixes = {e.unique_id.split("light.kitchen_", 1)[-1] for e in sensors}
    assert suffixes == {
        "lifetime_hours",
        "connected_hours",
        "lifetime_cycles",
        "connection_drops",
        "duty_cycle_pct",
        "flap_rate_1h",
        "flap_rate_24h",
        "unavail_rate_1h",
    }
    # The health-alert binary sensor is created too.
    health = [
        e
        for e in ent_reg.entities.values()
        if e.platform == DOMAIN and e.domain == "binary_sensor"
    ]
    assert len(health) == 1
    assert health[0].unique_id.endswith("_health_alert")


async def test_rename_follows_the_entity(hass, enable_custom_integrations):
    await _register_light(hass, "kitchen", "uid-1")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    assert "light.kitchen" in coordinator._tracked

    er.async_get(hass).async_update_entity("light.kitchen", new_entity_id="light.dining")
    await hass.async_block_till_done()

    assert "light.dining" in coordinator._tracked
    assert "light.kitchen" not in coordinator._tracked
    assert coordinator._tracked["light.dining"].entity_id == "light.dining"


async def test_removal_pauses_tracking_but_keeps_history(hass, enable_custom_integrations):
    await _register_light(hass, "kitchen", "uid-1")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    meta_id = coordinator._tracked["light.kitchen"].id

    er.async_get(hass).async_remove("light.kitchen")
    await hass.async_block_till_done()

    assert "light.kitchen" not in coordinator._tracked
    assert await coordinator.async_get_summary(meta_id) is not None


async def test_flap_raises_health_alert_and_fires_event(hass, enable_custom_integrations):
    hass.states.async_set("light.kitchen", "off")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    fired = async_capture_events(hass, "wear_tracker.flap_anomaly")

    for i in range(10):  # induce a flap
        hass.states.async_set("light.kitchen", "on" if i % 2 == 0 else "off")
        await hass.async_block_till_done()

    await coordinator._run_rollup(int(time.time()))
    await hass.async_block_till_done()

    meta_id = coordinator._tracked["light.kitchen"].id
    assert coordinator.get_health(meta_id) is True
    assert len(fired) == 1

    health_entity = next(
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith("_health_alert")
    )
    assert hass.states.get(health_entity).state == "on"


async def test_unload_entry_shuts_down(hass, enable_custom_integrations):
    entry, _coordinator = await _setup_entry(
        hass, {"entities": [], "discovery_mode": "off"}
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
