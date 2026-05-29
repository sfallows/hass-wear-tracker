"""State-machine logic: domain mapping, transition table, debounce, accrual.

Runs under plain pytest (no Home Assistant needed) via the _bootstrap shim.
"""
from __future__ import annotations

from _bootstrap import state_machine

StateKind = state_machine.StateKind
EntityStateMachine = state_machine.EntityStateMachine
derive_logical_state = state_machine.derive_logical_state


class FakeState:
    """Minimal stand-in for homeassistant.core.State for derive_logical_state."""

    def __init__(self, entity_id, state, attributes=None):
        self.entity_id = entity_id
        self.state = state
        self.domain = entity_id.split(".", 1)[0]
        self.attributes = attributes or {}


# --- derive_logical_state ---------------------------------------------------

def test_none_and_unavailable_are_disconnected():
    assert derive_logical_state(None) is StateKind.DISCONNECTED
    assert derive_logical_state(FakeState("light.a", "unavailable")) is StateKind.DISCONNECTED
    assert derive_logical_state(FakeState("light.a", "unknown")) is StateKind.DISCONNECTED


def test_light_switch_fan_on_off():
    for domain in ("light", "switch", "fan"):
        assert derive_logical_state(FakeState(f"{domain}.x", "on")) is StateKind.ON
        assert derive_logical_state(FakeState(f"{domain}.x", "off")) is StateKind.OFF


def test_climate_keys_on_hvac_action():
    heating = FakeState("climate.t", "heat", {"hvac_action": "heating"})
    idle = FakeState("climate.t", "heat", {"hvac_action": "idle"})
    assert derive_logical_state(heating) is StateKind.ON
    assert derive_logical_state(idle) is StateKind.OFF


def test_climate_mode_fallback_when_no_hvac_action():
    state_machine._WARNED_NO_HVAC_ACTION.clear()
    on = FakeState("climate.nohvac", "heat", {})
    off = FakeState("climate.nohvac", "off", {})
    assert derive_logical_state(on) is StateKind.ON
    assert derive_logical_state(off) is StateKind.OFF
    # Warning is one-shot per entity.
    assert "climate.nohvac" in state_machine._WARNED_NO_HVAC_ACTION


def test_cover_counts_motor_motion_only():
    assert derive_logical_state(FakeState("cover.c", "opening")) is StateKind.ON
    assert derive_logical_state(FakeState("cover.c", "closing")) is StateKind.ON
    assert derive_logical_state(FakeState("cover.c", "open")) is StateKind.OFF
    assert derive_logical_state(FakeState("cover.c", "closed")) is StateKind.OFF


def test_vacuum_and_media_player():
    assert derive_logical_state(FakeState("vacuum.v", "cleaning")) is StateKind.ON
    assert derive_logical_state(FakeState("vacuum.v", "docked")) is StateKind.OFF
    assert derive_logical_state(FakeState("media_player.m", "playing")) is StateKind.ON
    assert derive_logical_state(FakeState("media_player.m", "paused")) is StateKind.OFF


def test_water_heater_energized_vs_off():
    assert derive_logical_state(FakeState("water_heater.w", "eco")) is StateKind.ON
    assert derive_logical_state(FakeState("water_heater.w", "off")) is StateKind.OFF


# --- EntityStateMachine -----------------------------------------------------

def test_seed_writes_no_cycle_or_drop():
    sm = EntityStateMachine()
    ev = sm.observe("light.a", StateKind.ON, "on", 1000, 0.0)
    assert ev is not None
    assert ev.cycles_delta == 0 and ev.drops_delta == 0
    assert ev.lifetime_seconds_delta == 0.0


def test_same_state_is_noop():
    sm = EntityStateMachine()
    sm.observe("light.a", StateKind.OFF, "off", 1000, 0.0)
    assert sm.observe("light.a", StateKind.OFF, "off", 1001, 5.0) is None


def test_cycle_counted_only_past_debounce():
    sm = EntityStateMachine()
    sm.observe("light.a", StateKind.OFF, "off", 0, 0.0, 2.0)
    # off-period 1s < 2s debounce -> no cycle
    ev = sm.observe("light.a", StateKind.ON, "on", 1, 1.0, 2.0)
    assert ev.cycles_delta == 0
    sm.observe("light.a", StateKind.OFF, "off", 2, 2.0, 2.0)
    # off-period 5s >= debounce -> cycle
    ev = sm.observe("light.a", StateKind.ON, "on", 7, 7.0, 2.0)
    assert ev.cycles_delta == 1


def test_on_period_accrues_lifetime():
    sm = EntityStateMachine()
    sm.observe("light.a", StateKind.ON, "on", 0, 0.0)
    ev = sm.observe("light.a", StateKind.OFF, "off", 100, 100.0)
    assert ev.lifetime_seconds_delta == 100.0


def test_disconnect_drops_and_closes_connected():
    sm = EntityStateMachine()
    sm.observe("light.a", StateKind.ON, "on", 0, 0.0)
    ev = sm.observe("light.a", StateKind.DISCONNECTED, "unavailable", 50, 50.0)
    assert ev.drops_delta == 1
    assert ev.connected_seconds_delta == 50.0
    assert ev.lifetime_seconds_delta == 50.0


def test_connected_accrues_for_always_connected_device_via_flush():
    """Regression for the #1 bug: connected time must grow without a disconnect."""
    sm = EntityStateMachine()
    sm.observe("light.a", StateKind.ON, "on", 0, 0.0)
    deltas = sm.flush(60.0)
    assert len(deltas) == 1
    assert deltas[0].connected_seconds_delta == 60.0
    assert deltas[0].lifetime_seconds_delta == 60.0


def test_duty_invariant_connected_ge_lifetime():
    """connected_seconds must never fall below lifetime_seconds (duty <= 100%)."""
    sm = EntityStateMachine()
    life = conn = 0.0

    def apply(ev):
        nonlocal life, conn
        if ev:
            life += ev.lifetime_seconds_delta
            conn += ev.connected_seconds_delta

    def apply_flush(t):
        nonlocal life, conn
        for d in sm.flush(t):
            life += d.lifetime_seconds_delta
            conn += d.connected_seconds_delta

    apply(sm.observe("light.a", StateKind.ON, "on", 0, 0.0))
    apply(sm.observe("light.a", StateKind.DISCONNECTED, "unavailable", 100, 100.0))
    apply(sm.observe("light.a", StateKind.ON, "on", 110, 110.0))
    apply_flush(120.0)
    apply(sm.observe("light.a", StateKind.OFF, "off", 190, 190.0))
    apply_flush(250.0)
    assert conn + 1e-9 >= life


def test_rename_key_moves_machine():
    sm = EntityStateMachine()
    sm.observe("light.old", StateKind.ON, "on", 0, 0.0)
    sm.rename_key("light.old", "light.new")
    assert "light.new" in sm.snapshot()
    assert "light.old" not in sm.snapshot()
    # The moved machine keeps its state: a duplicate ON is still a noop.
    assert sm.observe("light.new", StateKind.ON, "on", 10, 10.0) is None
