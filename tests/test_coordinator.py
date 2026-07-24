"""Coordinator + config-entry setup through real Home Assistant.

Requires the HA test harness (pytest-homeassistant-custom-component); skipped
otherwise. Accrual/rename/unique_id *logic* is covered without HA in
test_state_machine.py and test_storage.py.
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import (  # noqa: E402
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    EntityCategory,
)
from homeassistant.core import CoreState  # noqa: E402
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


async def test_reregistration_resumes_tracking(hass, enable_custom_integrations):
    """DESIGN §4: a removed (soft-disabled) entity resumes counting when the same
    unique_id re-registers and the entry reloads — counters must not stay frozen."""
    await _register_light(hass, "kitchen", "uid-1")
    entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    meta_id = coordinator._tracked["light.kitchen"].id

    er.async_get(hass).async_remove("light.kitchen")
    await hass.async_block_till_done()
    assert "light.kitchen" not in coordinator._tracked

    # Same hardware re-pairs (same unique_id); a reload re-runs the upsert path.
    await _register_light(hass, "kitchen", "uid-1")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]

    tracked = coordinator.get_tracked("light.kitchen")
    assert tracked is not None
    assert tracked.id == meta_id  # same history row, not a fresh one
    assert tracked.disabled is False

    # Counting resumes: a fresh state change is now persisted (a disabled row would
    # be skipped by _handle_state_change and last_state would stay ON).
    hass.states.async_set("light.kitchen", "off")
    await hass.async_block_till_done()
    summary = await coordinator.async_get_summary(meta_id)
    assert summary["last_state"] == "OFF"


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


async def test_ha_stop_awaits_async_shutdown(hass, enable_custom_integrations):
    """Finding 1: the STOP listener must be awaited so the final flush runs."""
    _entry, coordinator = await _setup_entry(
        hass, {"entities": [], "discovery_mode": "off"}
    )
    assert coordinator._shutdown_started is False

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert coordinator._shutdown_started is True
    assert coordinator.writer._closed is True


async def test_entity_removed_from_state_machine_stops_accrual(hass, enable_custom_integrations):
    """Finding 2: new_state=None (source integration dropped the entity) must
    transition to DISCONNECTED and record a drop, not keep accruing."""
    hass.states.async_set("light.kitchen", "on")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    meta_id = coordinator._tracked["light.kitchen"].id
    assert str(coordinator.state_machine.snapshot()["light.kitchen"]) == "ON"

    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()

    assert str(coordinator.state_machine.snapshot()["light.kitchen"]) == "DISCONNECTED"
    summary = await coordinator.async_get_summary(meta_id)
    assert summary["last_state"] == "DISCONNECTED"
    assert summary["connection_drops"] == 1


async def test_failed_accrual_flush_is_retried_next_tick(
    hass, enable_custom_integrations, monkeypatch
):
    """Finding 5: a failed persist must not drop already-flushed on-time; the
    deltas are re-queued and retried on the next heartbeat."""
    _entry, coordinator = await _setup_entry(
        hass, {"entities": [], "discovery_mode": "off"}
    )
    monkeypatch.setattr(coordinator, "_collect_accruals", lambda _mono: [(1, 10.0, 10.0)])

    persisted: list[list[tuple[int, float, float]]] = []
    remaining_failures = {"n": 1}

    async def fake_apply(accruals, _ts):
        if remaining_failures["n"] > 0:
            remaining_failures["n"] -= 1
            raise RuntimeError("disk full")
        persisted.append(list(accruals))

    monkeypatch.setattr(coordinator.writer, "apply_accruals", fake_apply)

    await coordinator._handle_heartbeat(None)
    assert coordinator._pending_accruals == [(1, 10.0, 10.0)]
    assert persisted == []

    await coordinator._handle_heartbeat(None)
    assert coordinator._pending_accruals == []
    # Both the stashed delta and the fresh one land in one write.
    assert persisted == [[(1, 10.0, 10.0), (1, 10.0, 10.0)]]


async def test_seeding_deferred_until_ha_started(hass, enable_custom_integrations):
    """Findings 6 & 7: seeding is gated on HA-started so it sees real source
    states (no spurious cycle), and a fresh heartbeat is stamped afterwards."""
    hass.set_state(CoreState.not_running)
    before = int(time.time())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"entities": ["light.kitchen"], "discovery_mode": "off"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Deferred: entity is tracked but no machine seeded while HA is still starting.
    assert "light.kitchen" in coordinator.tracked_entity_ids()
    assert coordinator.state_machine.snapshot() == {}

    # Source integration publishes its real state, then HA finishes starting.
    hass.states.async_set("light.kitchen", "on")
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    snapshot = coordinator.state_machine.snapshot()
    assert str(snapshot["light.kitchen"]) == "ON"
    summary = await coordinator.async_get_summary(coordinator._tracked["light.kitchen"].id)
    assert summary["last_state"] == "ON"
    assert summary["lifetime_cycles"] == 0  # bootstrap seed writes no cycle

    # Finding 7: last_alive stamped at startup, not left for the 60s heartbeat.
    last_alive = await coordinator.writer.load_last_alive()
    assert last_alive is not None and last_alive >= before


async def test_shutdown_during_seed_leaves_no_heartbeat_timer(
    hass, enable_custom_integrations, monkeypatch
):
    """Shutdown racing the deferred seed must not leak the heartbeat interval
    the seed installs after its awaits (intermittent lingering-timer on reload)."""
    from custom_components.wear_tracker import storage

    seed_paused = asyncio.Event()
    release = asyncio.Event()
    orig_heartbeat = storage.AsyncSqliteWriter.heartbeat

    async def paused_heartbeat(self, ts):
        seed_paused.set()
        await release.wait()
        await orig_heartbeat(self, ts)

    monkeypatch.setattr(storage.AsyncSqliteWriter, "heartbeat", paused_heartbeat)

    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"entities": [], "discovery_mode": "off"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await asyncio.wait_for(seed_paused.wait(), timeout=1)
    shutdown = asyncio.ensure_future(coordinator.async_shutdown())
    for _ in range(3):
        await asyncio.sleep(0)
    release.set()
    await shutdown

    assert coordinator._unsub_heartbeat is None
    await hass.async_block_till_done()
