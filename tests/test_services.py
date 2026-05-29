"""Admin service round-trips through real Home Assistant."""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import SupportsResponse  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

DOMAIN = "wear_tracker"


async def _setup(hass, entities=("light.kitchen",)):
    for eid in entities:
        hass.states.async_set(eid, "on")
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"entities": list(entities), "discovery_mode": "off"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]


async def test_services_are_registered(hass, enable_custom_integrations):
    await _setup(hass)
    for service in ("reset", "set_rated", "export_log", "recompute", "disable", "purge", "purge_all"):
        assert hass.services.has_service(DOMAIN, service)


async def test_set_rated_then_reset(hass, enable_custom_integrations):
    _entry, coordinator = await _setup(hass)
    await hass.services.async_call(
        DOMAIN, "set_rated", {"entity_id": "light.kitchen", "hours": 1000}, blocking=True
    )
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]
    assert coordinator.get_tracked("light.kitchen").rated_hours == 1000

    await hass.services.async_call(
        DOMAIN, "reset", {"entity_id": "light.kitchen"}, blocking=True
    )
    await hass.async_block_till_done()


async def test_recompute_returns_list(hass, enable_custom_integrations):
    await _setup(hass)
    result = await hass.services.async_call(
        DOMAIN,
        "recompute",
        {"entity_id": "light.kitchen"},
        blocking=True,
        return_response=True,
    )
    assert result == {"recomputed": ["light.kitchen"]}


async def test_export_log_writes_csv(hass, enable_custom_integrations):
    await _setup(hass)
    result = await hass.services.async_call(
        DOMAIN,
        "export_log",
        {
            "entity_id": "light.kitchen",
            "start": "2020-01-01T00:00:00+00:00",
            "end": "2099-01-01T00:00:00+00:00",
            "filename": "kitchen.csv",
        },
        blocking=True,
        return_response=True,
    )
    assert result["path"].endswith("/wear_tracker/exports/kitchen.csv")
    assert result["rows"] >= 1  # at least the seed transition


async def test_export_log_rejects_path_traversal(hass, enable_custom_integrations):
    await _setup(hass)
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            "export_log",
            {
                "entity_id": "light.kitchen",
                "start": "2020-01-01T00:00:00+00:00",
                "end": "2099-01-01T00:00:00+00:00",
                "filename": "../escape.csv",
            },
            blocking=True,
            return_response=True,
        )


async def test_purge_all_requires_confirm(hass, enable_custom_integrations):
    await _setup(hass)
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN, "purge_all", {"confirm": False}, blocking=True, return_response=True
        )

    result = await hass.services.async_call(
        DOMAIN, "purge_all", {"confirm": True}, blocking=True, return_response=True
    )
    assert result["purged"] >= 1
