"""Admin service round-trips through real Home Assistant."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import SupportsResponse  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
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


async def test_purge_all_does_not_resurrect_entities(hass, enable_custom_integrations):
    """DESIGN §9: purge_all is irreversible. The post-purge reload must not
    re-upsert zeroed rows from a stale CONF_ENTITIES and resurrect the devices."""
    entry, _coordinator = await _setup(hass, entities=("light.kitchen", "switch.fan"))

    await hass.services.async_call(
        DOMAIN, "purge_all", {"confirm": True}, blocking=True, return_response=True
    )
    # No async_block_till_done: the service must have awaited the reload itself,
    # so the coordinator it leaves behind is already purge-free.
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.tracked_entity_ids() == []
    assert entry.options["entities"] == []


async def test_purge_all_auto_track_does_not_resurrect(hass, enable_custom_integrations):
    """In auto_track, scan_trackable would re-discover every still-existing entity
    on the post-purge reload; purge_all must exclude them all so it sticks."""
    er.async_get(hass).async_get_or_create(
        "switch", "demo", "uid-pump", suggested_object_id="pump"
    )
    hass.states.async_set("switch.pump", "on")
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"entities": [], "discovery_mode": "auto_track"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert "switch.pump" in coordinator.tracked_entity_ids()

    await hass.services.async_call(
        DOMAIN, "purge_all", {"confirm": True}, blocking=True, return_response=True
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.tracked_entity_ids() == []
    assert "switch.pump" in entry.options.get("excluded_entities", [])


async def test_purge_returns_after_new_coordinator_live(hass, enable_custom_integrations):
    """Finding 2: a blocking purge must not return while the old coordinator (with
    the purged entity still tracked) is live. Assert the purge-free coordinator is
    already in place, without an extra async_block_till_done after the call."""
    entry, _coordinator = await _setup(hass, entities=("light.kitchen", "switch.fan"))

    await hass.services.async_call(
        DOMAIN, "purge", {"entity_id": "light.kitchen"}, blocking=True
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.get_tracked("light.kitchen") is None
    assert coordinator.get_tracked("switch.fan") is not None


async def test_reset_clears_only_own_lts(hass, enable_custom_integrations):
    # switch.pump's unique-id root is a prefix of switch.pump_2's (HA's duplicate
    # naming); resetting switch.pump must not wipe switch.pump_2's statistics.
    await _setup(hass, entities=("switch.pump", "switch.pump_2"))
    coordinator = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]

    cleared: list[str] = []
    recorder_instance = MagicMock()
    recorder_instance.async_clear_statistics.side_effect = cleared.extend

    from custom_components.wear_tracker.services import _clear_lts

    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=recorder_instance,
    ):
        await _clear_lts(hass, coordinator, "switch.pump")

    reg = er.async_get(hass)
    cleared_uids = {reg.async_get(eid).unique_id for eid in cleared}

    assert "wear_tracker_switch.pump_lifetime_hours" in cleared_uids
    assert not any(uid.startswith("wear_tracker_switch.pump_2_") for uid in cleared_uids)


async def test_purge_does_not_resurrect_entity(hass, enable_custom_integrations):
    entry, _coordinator = await _setup(hass, entities=("light.kitchen", "switch.fan"))
    await hass.services.async_call(
        DOMAIN, "purge", {"entity_id": "light.kitchen"}, blocking=True
    )
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]
    assert coordinator.get_tracked("light.kitchen") is None
    assert coordinator.get_tracked("switch.fan") is not None
    assert "light.kitchen" not in entry.options["entities"]


async def test_purge_in_auto_track_excludes_entity(hass, enable_custom_integrations):
    """Residual purge finding: in auto_track, scan_trackable would re-discover a
    still-existing entity on the post-purge reload; purge must exclude it to stick."""
    er.async_get(hass).async_get_or_create(
        "switch", "demo", "uid-pump", suggested_object_id="pump"
    )
    hass.states.async_set("switch.pump", "on")
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"entities": [], "discovery_mode": "auto_track"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert "switch.pump" in coordinator.tracked_entity_ids()

    await hass.services.async_call(
        DOMAIN, "purge", {"entity_id": "switch.pump"}, blocking=True
    )
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert "switch.pump" not in coordinator.tracked_entity_ids()
    assert "switch.pump" in entry.options.get("excluded_entities", [])


async def test_purge_triggers_single_setup_cycle(hass, enable_custom_integrations, monkeypatch):
    """F15: purge runs its own awaited reload and suppresses the update listener's
    duplicate reload, so it costs exactly one setup cycle (not two). The purge
    guarantees still hold."""
    from custom_components.wear_tracker.coordinator import WearCoordinator

    entry, _coordinator = await _setup(hass, entities=("light.kitchen", "switch.fan"))

    calls = {"n": 0}
    orig_start = WearCoordinator.async_start

    async def counting_start(self, entity_ids):
        calls["n"] += 1
        return await orig_start(self, entity_ids)

    monkeypatch.setattr(WearCoordinator, "async_start", counting_start)

    await hass.services.async_call(
        DOMAIN, "purge", {"entity_id": "light.kitchen"}, blocking=True
    )
    await hass.async_block_till_done()

    assert calls["n"] == 1  # exactly one setup cycle, not the listener + explicit two
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.get_tracked("light.kitchen") is None
    assert coordinator.get_tracked("switch.fan") is not None
    assert "light.kitchen" not in entry.options["entities"]


async def test_purge_all_triggers_single_setup_cycle(hass, enable_custom_integrations, monkeypatch):
    """F15: purge_all likewise costs a single setup cycle while still clearing the
    tracked options so nothing resurrects."""
    from custom_components.wear_tracker.coordinator import WearCoordinator

    entry, _coordinator = await _setup(hass, entities=("light.kitchen", "switch.fan"))

    calls = {"n": 0}
    orig_start = WearCoordinator.async_start

    async def counting_start(self, entity_ids):
        calls["n"] += 1
        return await orig_start(self, entity_ids)

    monkeypatch.setattr(WearCoordinator, "async_start", counting_start)

    await hass.services.async_call(
        DOMAIN, "purge_all", {"confirm": True}, blocking=True, return_response=True
    )
    await hass.async_block_till_done()

    assert calls["n"] == 1
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.tracked_entity_ids() == []
    assert entry.options["entities"] == []
