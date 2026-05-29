"""Per-entity state machine + domain → logical-state mapping.

See DESIGN.md §4 for the per-domain mapping rationale and §5 for `delta_s`
semantics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import State

_LOG = logging.getLogger(__name__)

_HA_UNAVAILABLE = "unavailable"
_HA_UNKNOWN = "unknown"

# Climate entities that fell back to mode-based on-time (no hvac_action exposed),
# tracked so the warning fires only once per entity.
_WARNED_NO_HVAC_ACTION: set[str] = set()


class StateKind(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    OFF = "OFF"
    ON = "ON"


_CLIMATE_ON_ACTIONS = frozenset(
    {"heating", "cooling", "drying", "fan", "preheating", "defrosting"}
)
_CLIMATE_OFF_ACTIONS = frozenset({"idle", "off"})
_CLIMATE_ON_MODES = frozenset({"heat", "cool", "heat_cool", "auto", "dry", "fan_only"})
_VACUUM_ON = frozenset({"cleaning", "returning"})
_MEDIA_ON = frozenset({"playing", "buffering"})
_COVER_MOTOR_ACTIVE = frozenset({"opening", "closing"})


def derive_logical_state(state: State | None) -> StateKind:
    """Map a HA `State` (raw state + attributes) → logical wear-tracker state."""
    if state is None:
        return StateKind.DISCONNECTED

    raw = state.state
    if raw in (None, _HA_UNAVAILABLE, _HA_UNKNOWN):
        return StateKind.DISCONNECTED

    domain = state.domain

    if domain == "climate":
        action = state.attributes.get("hvac_action")
        if action in _CLIMATE_ON_ACTIONS:
            return StateKind.ON
        if action in _CLIMATE_OFF_ACTIONS:
            return StateKind.OFF
        if action is None and state.entity_id not in _WARNED_NO_HVAC_ACTION:
            _WARNED_NO_HVAC_ACTION.add(state.entity_id)
            _LOG.warning(
                "wear_tracker: %s exposes no hvac_action; accruing mode-based "
                "(energized) on-time rather than active heating/cooling. Pair with a "
                "power sensor for a true duty cycle.",
                state.entity_id,
            )
        if raw == "off":
            return StateKind.OFF
        if raw in _CLIMATE_ON_MODES:
            return StateKind.ON
        return StateKind.OFF

    if domain == "water_heater":
        return StateKind.OFF if raw == "off" else StateKind.ON

    if domain == "cover":
        return StateKind.ON if raw in _COVER_MOTOR_ACTIVE else StateKind.OFF

    if domain == "vacuum":
        return StateKind.ON if raw in _VACUUM_ON else StateKind.OFF

    if domain == "media_player":
        return StateKind.ON if raw in _MEDIA_ON else StateKind.OFF

    return StateKind.ON if raw == "on" else StateKind.OFF


@dataclass(slots=True)
class TransitionEvent:
    """A transition the storage layer should persist atomically.

    Summary deltas are pre-computed so the writer applies one transactional
    update covering both `transitions` and `summary`.
    """

    entity_id: str
    ts: int
    from_state: StateKind
    to_state: StateKind
    raw_from: str | None
    raw_to: str | None
    delta_s: float
    lifetime_seconds_delta: float
    connected_seconds_delta: float
    cycles_delta: int
    drops_delta: int


@dataclass(slots=True)
class AccrualDelta:
    """In-progress on/connected time to fold into `summary` between transitions.

    Produced by `EntityStateMachine.flush()` on the rollup tick and on shutdown
    so the counters reflect periods that haven't closed yet (a light left on for
    days, a device that never disconnects). No transition row is written for it.
    """

    entity_id: str
    lifetime_seconds_delta: float
    connected_seconds_delta: float


@dataclass(slots=True)
class _Machine:
    current: StateKind | None = None
    raw_current: str | None = None
    state_entered_monotonic: float = 0.0
    # Monotonic point up to which on-time / connected-time has been credited to
    # summary. Advanced on every transition and on every flush; the gap up to
    # `time.monotonic()` is the as-yet-uncredited remainder of the open period.
    lifetime_credited_monotonic: float = 0.0
    connected_credited_monotonic: float = 0.0
    in_connected_period: bool = False


class EntityStateMachine:
    """In-memory per-entity state. Produces `TransitionEvent`s for the writer."""

    def __init__(self) -> None:
        self._machines: dict[str, _Machine] = {}

    def observe(
        self,
        entity_id: str,
        new_state: StateKind,
        raw_state: str | None,
        ts: int,
        monotonic: float,
        debounce_s: float = 2.0,
    ) -> TransitionEvent | None:
        """Process one observation. Returns the event to persist, or `None` for noops."""
        m = self._machines.setdefault(entity_id, _Machine())

        if m.current is None:
            m.current = new_state
            m.raw_current = raw_state
            m.state_entered_monotonic = monotonic
            if new_state is StateKind.ON:
                m.lifetime_credited_monotonic = monotonic
            if new_state is not StateKind.DISCONNECTED:
                m.connected_credited_monotonic = monotonic
                m.in_connected_period = True
            return TransitionEvent(
                entity_id=entity_id,
                ts=ts,
                from_state=StateKind.DISCONNECTED,
                to_state=new_state,
                raw_from=None,
                raw_to=raw_state,
                delta_s=0.0,
                lifetime_seconds_delta=0.0,
                connected_seconds_delta=0.0,
                cycles_delta=0,
                drops_delta=0,
            )

        old_state = m.current
        if old_state is new_state:
            return None

        period_s = max(0.0, monotonic - m.state_entered_monotonic)

        lifetime_delta = 0.0
        connected_delta = 0.0
        cycles_delta = 0
        drops_delta = 0

        if old_state is StateKind.ON:
            lifetime_delta = max(0.0, monotonic - m.lifetime_credited_monotonic)

        # Credit connected time accrued so far in the current connected period on
        # *every* transition (not only on disconnect). This keeps connected_seconds
        # in lockstep with lifetime_seconds so duty cycle can never exceed 100%.
        if m.in_connected_period:
            connected_delta = max(0.0, monotonic - m.connected_credited_monotonic)
            m.connected_credited_monotonic = monotonic

        if new_state is StateKind.ON and old_state in (StateKind.OFF, StateKind.DISCONNECTED):
            if period_s >= debounce_s:
                cycles_delta = 1

        if new_state is StateKind.DISCONNECTED:
            drops_delta = 1
            m.in_connected_period = False
        elif old_state is StateKind.DISCONNECTED:
            m.in_connected_period = True
            m.connected_credited_monotonic = monotonic

        event = TransitionEvent(
            entity_id=entity_id,
            ts=ts,
            from_state=old_state,
            to_state=new_state,
            raw_from=m.raw_current,
            raw_to=raw_state,
            delta_s=period_s,
            lifetime_seconds_delta=lifetime_delta,
            connected_seconds_delta=connected_delta,
            cycles_delta=cycles_delta,
            drops_delta=drops_delta,
        )

        m.current = new_state
        m.raw_current = raw_state
        m.state_entered_monotonic = monotonic
        if new_state is StateKind.ON:
            m.lifetime_credited_monotonic = monotonic

        return event

    def flush(self, monotonic: float) -> list[AccrualDelta]:
        """Credit in-progress on/connected time without closing any state period.

        Called on the rollup tick and on shutdown so long-running periods are
        reflected in `summary` instead of only being written when the period
        finally closes. Advances each machine's credit anchors to `monotonic`.
        """
        out: list[AccrualDelta] = []
        for entity_id, m in self._machines.items():
            if m.current is None:
                continue
            lifetime_delta = 0.0
            connected_delta = 0.0
            if m.current is StateKind.ON:
                lifetime_delta = max(0.0, monotonic - m.lifetime_credited_monotonic)
                m.lifetime_credited_monotonic = monotonic
            if m.in_connected_period:
                connected_delta = max(0.0, monotonic - m.connected_credited_monotonic)
                m.connected_credited_monotonic = monotonic
            if lifetime_delta > 0.0 or connected_delta > 0.0:
                out.append(AccrualDelta(entity_id, lifetime_delta, connected_delta))
        return out

    def reset(self, entity_id: str) -> None:
        self._machines.pop(entity_id, None)

    def rename_key(self, old_entity_id: str, new_entity_id: str) -> None:
        machine = self._machines.pop(old_entity_id, None)
        if machine is not None:
            self._machines[new_entity_id] = machine

    def snapshot(self) -> dict[str, StateKind | None]:
        return {eid: m.current for eid, m in self._machines.items()}
