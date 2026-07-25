"""Coordinator + config-entry setup through real Home Assistant.

Requires the HA test harness (pytest-homeassistant-custom-component); skipped
otherwise. Accrual/rename/unique_id *logic* is covered without HA in
test_state_machine.py and test_storage.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
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
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
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
    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    assert "light.kitchen" not in coordinator._tracked

    # Same hardware re-pairs, reclaiming the same entity_id and unique_id; the
    # reload upsert must match by identity (platform, domain, unique_id) and
    # resume — a bare entity_id match must not (finding 2).
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


async def test_setup_migrates_old_sensor_unique_ids(hass, enable_custom_integrations):
    """Finding 1 (highest severity): on upgrade the pre-composite sensor
    unique_ids (rooted on the bare unique_id) are rewritten in place to the new
    platform-qualified root, so existing sensors keep their entity_id, history and
    long-term statistics instead of being re-registered as duplicates."""
    await _register_light(hass, "kitchen", "uid-1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"entities": ["light.kitchen"], "discovery_mode": "off"},
    )
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    old = reg.async_get_or_create(
        "sensor", DOMAIN, "wear_tracker_uid-1_lifetime_hours",
        suggested_object_id="kitchen_lifetime_hours", config_entry=entry,
    )
    old_entity_id = old.entity_id

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Rewritten in place: same entity_id, new platform-qualified unique_id.
    migrated = reg.async_get(old_entity_id)
    assert migrated is not None
    assert migrated.unique_id == "wear_tracker_demo_light_uid-1_lifetime_hours"
    # No duplicate lifetime_hours sensor was created for this entity.
    lifetime = [
        e
        for e in reg.entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith("_lifetime_hours")
    ]
    assert len(lifetime) == 1
    assert lifetime[0].entity_id == old_entity_id


async def test_setup_migration_skips_when_target_uid_exists(hass, enable_custom_integrations):
    """The unique_id migration must not crash when the new-format unique_id is
    already taken (e.g. a partially-migrated install); the old entry is left as-is
    rather than triggering a collision in async_update_entity."""
    await _register_light(hass, "kitchen", "uid-1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"entities": ["light.kitchen"], "discovery_mode": "off"},
    )
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    old = reg.async_get_or_create(
        "sensor", DOMAIN, "wear_tracker_uid-1_lifetime_hours",
        suggested_object_id="kitchen_old", config_entry=entry,
    )
    # The target new-format unique_id already exists on a different entity.
    reg.async_get_or_create(
        "sensor", DOMAIN, "wear_tracker_demo_light_uid-1_lifetime_hours",
        suggested_object_id="kitchen_new", config_entry=entry,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)  # must not raise
    await hass.async_block_till_done()

    # The guard left the old entry on its original unique_id.
    assert reg.async_get(old.entity_id).unique_id == "wear_tracker_uid-1_lifetime_hours"


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
    # The seed installs the state subscription at its start now; a shutdown that
    # interleaves the seed's awaits must still have torn it down (finding 3).
    assert coordinator._unsub_state is None
    await hass.async_block_till_done()


async def test_rename_of_on_entity_records_no_drop_and_keeps_accruing(
    hass, enable_custom_integrations
):
    """Finding 1: a rename removes the old entity_id's state; when that None event
    (a synchronous callback) lands before the queued reconcile, it must not park the
    machine DISCONNECTED and record a spurious drop. The machine follows the rename
    and keeps accruing."""
    await _register_light(hass, "kitchen", "uid-1")  # state on
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    meta_id = coordinator._tracked["light.kitchen"].id
    assert str(coordinator.state_machine.snapshot()["light.kitchen"]) == "ON"
    before = await coordinator.async_get_summary(meta_id)

    # Simulate HA's rename: the ER entry moves, then the old id's state is removed
    # and the new id's appears. The reconcile is a queued task; the state-change
    # callbacks below run synchronously first (the remove-state-first ordering).
    er.async_get(hass).async_update_entity(
        "light.kitchen", new_entity_id="light.dining"
    )
    hass.states.async_remove("light.kitchen")
    hass.states.async_set("light.dining", "on")
    await hass.async_block_till_done()

    assert "light.dining" in coordinator._tracked
    assert "light.kitchen" not in coordinator._tracked
    snapshot = coordinator.state_machine.snapshot()
    assert str(snapshot["light.dining"]) == "ON"
    assert "light.kitchen" not in snapshot
    after = await coordinator.async_get_summary(meta_id)
    assert after["last_state"] == "ON"
    assert after["connection_drops"] == before["connection_drops"]


async def test_registry_churn_before_started_defers_subscription(
    hass, enable_custom_integrations
):
    """Finding 2: registry handlers must not install the state subscription before
    the seed runs, or boot-time churn fabricates cycles/drops from pre-started
    states on unseeded machines. The seed installs it once HA has started."""
    hass.set_state(CoreState.not_running)
    await _register_light(hass, "kitchen", "uid-1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"entities": ["light.kitchen"], "discovery_mode": "off"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator._unsub_state is None
    assert coordinator._seeded is False

    # A rename arrives during boot churn (before HA-started).
    er.async_get(hass).async_update_entity(
        "light.kitchen", new_entity_id="light.dining"
    )
    await hass.async_block_till_done()

    # Tracking was re-keyed, but the subscription was NOT installed pre-seed.
    assert "light.dining" in coordinator._tracked
    assert coordinator._unsub_state is None
    assert coordinator.state_machine.snapshot() == {}

    # HA finishes starting; the seed installs the subscription and observes state.
    hass.states.async_set("light.dining", "on")
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert coordinator._seeded is True
    assert coordinator._unsub_state is not None
    assert str(coordinator.state_machine.snapshot()["light.dining"]) == "ON"


async def test_restart_gap_credit_gated_on_setup_ts_not_started(
    hass, enable_custom_integrations
):
    """Finding 4: the restart-gap tolerance is measured against the offline gap
    captured at entry setup, not seed-now, so a slow boot-to-started delay can't
    blow the tolerance and silently skip the credit."""
    from custom_components.wear_tracker.const import (
        RESTART_CREDIT_MAX_S,
        RESTART_GAP_TOLERANCE_S,
    )
    from custom_components.wear_tracker.state_machine import StateKind

    hass.states.async_set("light.kitchen", "on")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    tracked = coordinator._tracked["light.kitchen"]
    meta_id = tracked.id
    before = await coordinator.async_get_summary(meta_id)
    assert before["last_state"] == "ON"

    last_alive = 1_000_000
    # Window > tolerance (would blow the gate if measured at seed-now) but within the
    # credit cap, so this test isolates the gate from F9's cap.
    seed_now = last_alive + RESTART_GAP_TOLERANCE_S + 100
    assert seed_now - last_alive <= RESTART_CREDIT_MAX_S

    # Offline gap at setup within tolerance -> credit the full stayed-ON window.
    coordinator._setup_ts = last_alive + 10
    await coordinator._recover_open_period(tracked, StateKind.ON, seed_now, last_alive)
    after = await coordinator.async_get_summary(meta_id)
    assert after["lifetime_seconds"] - before["lifetime_seconds"] == float(
        seed_now - last_alive
    )
    assert after["connected_seconds"] - before["connected_seconds"] == float(
        seed_now - last_alive
    )

    # Offline gap beyond tolerance -> credit nothing.
    coordinator._setup_ts = last_alive + RESTART_GAP_TOLERANCE_S + 1
    await coordinator._recover_open_period(tracked, StateKind.ON, seed_now, last_alive)
    after2 = await coordinator.async_get_summary(meta_id)
    assert after2["lifetime_seconds"] == after["lifetime_seconds"]


async def test_seed_failure_retries_and_succeeds(
    hass, enable_custom_integrations, monkeypatch
):
    """Finding 5: a transient DB error during seeding is retried with backoff and
    the heartbeat timer installs once a retry succeeds, instead of freezing the
    integration until a manual reload."""
    from custom_components.wear_tracker import storage
    from custom_components.wear_tracker.const import SEED_RETRY_INITIAL_S

    orig_load = storage.AsyncSqliteWriter.load_last_alive
    calls = {"n": 0}

    async def flaky_load(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return await orig_load(self)

    monkeypatch.setattr(storage.AsyncSqliteWriter, "load_last_alive", flaky_load)

    hass.states.async_set("light.kitchen", "on")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )

    # First attempt failed: heartbeat timer not installed, a retry is scheduled.
    assert calls["n"] == 1
    assert coordinator._unsub_heartbeat is None
    assert coordinator._unsub_seed_retry is not None

    async_fire_time_changed(
        hass, dt_util.utcnow() + dt.timedelta(seconds=SEED_RETRY_INITIAL_S + 1)
    )
    await hass.async_block_till_done()

    # Retry succeeded: timer installed, retry handle cleared, machine seeded.
    assert calls["n"] == 2
    assert coordinator._unsub_heartbeat is not None
    assert coordinator._unsub_seed_retry is None
    assert str(coordinator.state_machine.snapshot()["light.kitchen"]) == "ON"


async def test_live_repair_resumes_tracking_without_restart(
    hass, enable_custom_integrations
):
    """Finding 6: a live registry remove then re-create for the same entity_id must
    resume counting via a scheduled reload. The create can't re-add an already
    configured entity_id, so without the reload tracking stays frozen until an HA
    restart — even in `off` discovery mode."""
    await _register_light(hass, "kitchen", "uid-1")
    entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    meta_id = coordinator._tracked["light.kitchen"].id

    er.async_get(hass).async_remove("light.kitchen")
    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    assert "light.kitchen" not in coordinator._tracked

    # Same hardware re-pairs live (registry create for the same entity_id + uid).
    await _register_light(hass, "kitchen", "uid-1")
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    tracked = coordinator.get_tracked("light.kitchen")
    assert tracked is not None
    assert tracked.id == meta_id  # resumed the same history row
    assert tracked.disabled is False

    # Counting resumes: a fresh state change is persisted.
    hass.states.async_set("light.kitchen", "off")
    await hass.async_block_till_done()
    summary = await coordinator.async_get_summary(meta_id)
    assert summary["last_state"] == "OFF"


async def _preseed_db_row(hass, entity_id, domain, *, unique_id=None, platform=None):
    """Create a wear_tracker DB row before the entry is set up, so a later setup
    reuses the same DB file (simulating a pre-existing/legacy install)."""
    from pathlib import Path

    from custom_components.wear_tracker import registry
    from custom_components.wear_tracker.const import DB_FILENAME, DB_SUBDIR
    from custom_components.wear_tracker.storage import AsyncSqliteWriter

    db_path = Path(hass.config.path(DB_SUBDIR)) / DB_FILENAME
    writer = AsyncSqliteWriter(db_path)
    await writer.open()
    try:
        return await writer.run(
            lambda c: registry.upsert(
                c, entity_id=entity_id, domain=domain, tracking_since=1,
                unique_id=unique_id, platform=platform,
            )
        )
    finally:
        await writer.close()


async def test_recover_open_period_caps_credit(hass, enable_custom_integrations):
    """F9: the recovered restart on-period is capped, so a huge boot/seed-retry
    window can't fabricate many minutes if the device was toggled while unobserved."""
    from custom_components.wear_tracker.const import RESTART_CREDIT_MAX_S
    from custom_components.wear_tracker.state_machine import StateKind

    hass.states.async_set("light.kitchen", "on")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    tracked = coordinator._tracked["light.kitchen"]
    meta_id = tracked.id
    before = await coordinator.async_get_summary(meta_id)

    last_alive = 1_000_000
    coordinator._setup_ts = last_alive + 10          # offline gap within tolerance
    seed_now = last_alive + 10_000                    # huge boot window
    await coordinator._recover_open_period(tracked, StateKind.ON, seed_now, last_alive)
    after = await coordinator.async_get_summary(meta_id)
    assert after["lifetime_seconds"] - before["lifetime_seconds"] == float(
        RESTART_CREDIT_MAX_S
    )


async def test_recover_open_period_credit_ends_at_bootstrap(hass, enable_custom_integrations):
    """F5 credit-end: when a live event bootstrapped the machine mid-seed, the
    restart credit ends at the bootstrap ts (no overlap with live accrual), not at
    seed-now."""
    from custom_components.wear_tracker.state_machine import StateKind

    hass.states.async_set("light.kitchen", "on")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    tracked = coordinator._tracked["light.kitchen"]
    meta_id = tracked.id
    before = await coordinator.async_get_summary(meta_id)

    last_alive = 1_000_000
    coordinator._setup_ts = last_alive + 10          # within tolerance
    boot_ts = last_alive + 40
    seed_now = last_alive + 200
    await coordinator._recover_open_period(
        tracked, StateKind.ON, seed_now, last_alive, credit_end=boot_ts
    )
    after = await coordinator.async_get_summary(meta_id)
    # 40 (last_alive -> bootstrap), not 200 (last_alive -> seed-now).
    assert after["lifetime_seconds"] - before["lifetime_seconds"] == float(
        boot_ts - last_alive
    )


async def test_live_bootstrap_midseed_still_credits_restart(
    hass, enable_custom_integrations, monkeypatch
):
    """F5 (defect i): a live event that bootstraps a machine mid-seed must NOT lose
    the restart on-time credit (the pre-fix machine-exists skip dropped it)."""
    from custom_components.wear_tracker.const import RESTART_CREDIT_MAX_S

    hass.set_state(CoreState.not_running)
    entry = MockConfigEntry(
        domain=DOMAIN, data={},
        options={"entities": ["light.kitchen"], "discovery_mode": "off"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    meta_id = coordinator._tracked["light.kitchen"].id

    last_alive = 1_000_000
    await coordinator.writer.run(
        lambda c: c.execute(
            "INSERT INTO summary (entity_meta_id, lifetime_seconds, connected_seconds,"
            " lifetime_cycles, connection_drops, last_state, last_change_ts, updated_ts)"
            " VALUES (?, 0, 0, 0, 0, 'ON', ?, ?)",
            (meta_id, last_alive, last_alive),
        )
    )
    await coordinator.writer.heartbeat(last_alive)
    coordinator._setup_ts = last_alive + 10  # offline gap within tolerance

    orig_load = coordinator.writer.load_last_alive

    async def injecting_load():
        # Fire a live ON event mid-seed (subscription already installed) so the
        # machine bootstraps before the seed loop reaches this entity.
        hass.states.async_set("light.kitchen", "on")
        return await orig_load()

    monkeypatch.setattr(coordinator.writer, "load_last_alive", injecting_load)

    before = await coordinator.async_get_summary(meta_id)
    assert before["lifetime_seconds"] == 0.0
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    after = await coordinator.async_get_summary(meta_id)
    # Credit applied (boot_ts - last_alive is huge -> capped), not lost; no
    # double-observe (bootstrap wrote no cycle).
    assert after["lifetime_seconds"] - before["lifetime_seconds"] == float(
        RESTART_CREDIT_MAX_S
    )
    assert after["lifetime_cycles"] == 0
    assert after["last_state"] == "ON"


async def test_seed_skips_disabled_rows(hass, enable_custom_integrations):
    """F5 (defect ii): the seed loop must skip disabled rows — a paused entity gets
    no machine (and so no restart credit)."""
    await _register_light(hass, "kitchen", "uid-1")
    entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    await hass.services.async_call(
        DOMAIN, "disable", {"entity_id": "light.kitchen", "disabled": True}, blocking=True
    )
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    tracked = coordinator.get_tracked("light.kitchen")
    assert tracked is not None and tracked.disabled is True
    assert "light.kitchen" not in coordinator.state_machine.snapshot()


async def test_rename_midseed_leaves_no_ghost_machine(
    hass, enable_custom_integrations, monkeypatch
):
    """F5 (defect iii): a rename mid-seed must not process the now-stale key — no
    ghost DISCONNECTED machine and no bogus transition on the renamed row's meta id."""
    hass.set_state(CoreState.not_running)
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "on")
    entry = MockConfigEntry(
        domain=DOMAIN, data={},
        options={"entities": ["light.a", "light.b"], "discovery_mode": "off"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    b_meta_id = coordinator._tracked["light.b"].id

    orig = coordinator._recover_open_period
    renamed = {"done": False}

    async def hooking(tracked, logical, now_ts, last_alive, credit_end=None):
        # On the first iteration (light.a), simulate a rename of light.b completing
        # mid-seed exactly as the real handler would: pop the old key and drop its
        # source state. The loop must then skip the stale light.b key.
        if not renamed["done"] and "light.b" in coordinator._tracked:
            renamed["done"] = True
            coordinator._tracked["light.c"] = coordinator._tracked.pop("light.b")
            coordinator._source_device["light.c"] = coordinator._source_device.pop(
                "light.b", None
            )
            hass.states.async_remove("light.b")
        return await orig(tracked, logical, now_ts, last_alive, credit_end=credit_end)

    monkeypatch.setattr(coordinator, "_recover_open_period", hooking)

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert renamed["done"] is True
    assert "light.b" not in coordinator.state_machine.snapshot()  # no ghost machine
    ghost_rows = await coordinator.writer.run(
        lambda c: c.execute(
            "SELECT COUNT(*) FROM transitions WHERE entity_meta_id=? AND to_state='DISCONNECTED'",
            (b_meta_id,),
        ).fetchone()[0]
    )
    assert ghost_rows == 0


async def test_er_entry_gone_then_state_drop_observes_disconnected(
    hass, enable_custom_integrations
):
    """F4/F12 phantom-ON: if the ER entry vanished without a coordinator-visible
    event, a later real state-drop must still observe DISCONNECTED — not be skipped
    forever as an in-flight rename (which would leave a phantom ON)."""
    await _register_light(hass, "kitchen", "uid-1")  # state on
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    assert str(coordinator.state_machine.snapshot()["light.kitchen"]) == "ON"

    # The coordinator misses the removal: drop its registry subscription, then remove
    # the ER entry. The entity stays tracked with an ON machine.
    coordinator._unsub_registry()
    coordinator._unsub_registry = None
    er.async_get(hass).async_remove("light.kitchen")
    await hass.async_block_till_done()
    assert coordinator.get_tracked("light.kitchen") is not None
    assert str(coordinator.state_machine.snapshot()["light.kitchen"]) == "ON"

    # The source later drops the state. No rename is in flight, so DISCONNECTED is
    # observed rather than skipped.
    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    assert str(coordinator.state_machine.snapshot()["light.kitchen"]) == "DISCONNECTED"


async def test_removal_of_on_entity_records_drop_registry_first(
    hass, enable_custom_integrations
):
    """F12: removing an ON entity (registry event first, state still present) records
    the final on-period close + drop and lands last_state DISCONNECTED."""
    await _register_light(hass, "kitchen", "uid-1")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    meta_id = coordinator._tracked["light.kitchen"].id
    assert str(coordinator.state_machine.snapshot()["light.kitchen"]) == "ON"
    before = await coordinator.async_get_summary(meta_id)

    er.async_get(hass).async_remove("light.kitchen")
    await hass.async_block_till_done()

    assert "light.kitchen" not in coordinator._tracked
    summary = await coordinator.async_get_summary(meta_id)
    assert summary["last_state"] == "DISCONNECTED"
    assert summary["connection_drops"] == before["connection_drops"] + 1


async def test_removal_of_on_entity_records_single_drop_state_first(
    hass, enable_custom_integrations
):
    """F12: state-removed-first then registry-remove records exactly one drop — the
    removed-handler's same-state DISCONNECTED observe is a noop."""
    await _register_light(hass, "kitchen", "uid-1")
    _entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    meta_id = coordinator._tracked["light.kitchen"].id
    before = await coordinator.async_get_summary(meta_id)

    reg = er.async_get(hass)
    hass.states.async_remove("light.kitchen")  # ER entry still present here
    reg.async_remove("light.kitchen")
    await hass.async_block_till_done()

    summary = await coordinator.async_get_summary(meta_id)
    assert summary["last_state"] == "DISCONNECTED"
    assert summary["connection_drops"] == before["connection_drops"] + 1


async def test_live_repair_after_restart_with_disabled_placeholder(
    hass, enable_custom_integrations
):
    """F11: after a restart between removal and re-pair, async_start puts the
    removal-disabled row back into _tracked; a re-registration must still schedule a
    reload (the disabled placeholder counts as untracked) and resume tracking."""
    await _register_light(hass, "kitchen", "uid-1")
    entry, coordinator = await _setup_entry(
        hass, {"entities": ["light.kitchen"], "discovery_mode": "off"}
    )
    meta_id = coordinator._tracked["light.kitchen"].id

    er.async_get(hass).async_remove("light.kitchen")
    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    assert "light.kitchen" not in coordinator._tracked

    # Simulate a restart while still removed: async_start re-inserts the disabled row
    # as a placeholder in _tracked.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    placeholder = coordinator.get_tracked("light.kitchen")
    assert placeholder is not None and placeholder.disabled is True

    # Same hardware re-pairs live: create must schedule a reload despite the disabled
    # placeholder in _tracked, resuming the same history row.
    await _register_light(hass, "kitchen", "uid-1")
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    tracked = coordinator.get_tracked("light.kitchen")
    assert tracked is not None
    assert tracked.id == meta_id and tracked.disabled is False


async def test_setup_platform_null_row_keeps_bare_unique_id_no_duplicate(
    hass, enable_custom_integrations
):
    """F13(a): a tracked row with a unique_id but platform still NULL keeps the
    pre-composite bare-unique_id sensor root — no remap, no *_2 duplicate."""
    await _preseed_db_row(hass, "light.kitchen", "light", unique_id="uid-1")
    hass.states.async_set("light.kitchen", "on")  # no ER entry -> platform stays NULL
    entry = MockConfigEntry(
        domain=DOMAIN, data={},
        options={"entities": ["light.kitchen"], "discovery_mode": "off"},
    )
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    old = reg.async_get_or_create(
        "sensor", DOMAIN, "wear_tracker_uid-1_lifetime_hours",
        suggested_object_id="kitchen_lifetime_hours", config_entry=entry,
    )
    old_entity_id = old.entity_id

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    tracked = coordinator.get_tracked("light.kitchen")
    assert tracked.unique_id == "uid-1" and tracked.platform is None
    # Kept on the bare-unique_id root; no qualified duplicate minted.
    assert reg.async_get(old_entity_id).unique_id == "wear_tracker_uid-1_lifetime_hours"
    lifetime = [
        e for e in reg.entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith("_lifetime_hours")
    ]
    assert len(lifetime) == 1


async def test_setup_remaps_entity_id_rooted_sensors_when_identity_appears(
    hass, enable_custom_integrations
):
    """F13(b): sensors registered under the entity_id root (a row that had no
    unique_id pre-upgrade) are remapped to the platform-qualified root once
    unique_id+platform are backfilled this boot."""
    await _preseed_db_row(hass, "light.kitchen", "light")  # legacy: no unique_id
    await _register_light(hass, "kitchen", "uid-1")  # ER identity appears this boot
    entry = MockConfigEntry(
        domain=DOMAIN, data={},
        options={"entities": ["light.kitchen"], "discovery_mode": "off"},
    )
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    old = reg.async_get_or_create(
        "sensor", DOMAIN, "wear_tracker_light.kitchen_lifetime_hours",
        suggested_object_id="kitchen_lifetime_hours", config_entry=entry,
    )
    old_entity_id = old.entity_id

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    migrated = reg.async_get(old_entity_id)
    assert migrated is not None
    assert migrated.unique_id == "wear_tracker_demo_light_uid-1_lifetime_hours"
    lifetime = [
        e for e in reg.entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith("_lifetime_hours")
    ]
    assert len(lifetime) == 1
    assert lifetime[0].entity_id == old_entity_id
