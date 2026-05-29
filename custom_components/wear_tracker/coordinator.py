"""WearCoordinator — event-driven owner class per config entry.

Plain class, not `DataUpdateCoordinator` — this integration is driven by HA
state-change events, not by polling. See DESIGN.md §2.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import Event, HomeAssistant, callback

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from . import events, registry, repairs, rollup, storage
from .catalog import lookup_rated
from .const import (
    ANOMALY_DEBOUNCE_S,
    BASELINE_FLOOR,
    BASELINE_REFRESH_S,
    BASELINE_WINDOW_DAYS,
    CONF_DISCOVERY_MODE,
    CONF_ENTITIES,
    CONF_EXCLUDED_DOMAINS,
    CONF_INCLUDE_BINARY_SENSORS,
    CONNECTION_MIN_PER_HOUR,
    CONNECTION_RATIO,
    DEFAULT_DEBOUNCE_S,
    DISCOVERY_AUTO_TRACK,
    DISCOVERY_PROMPT,
    EVENT_WEAR_CRITICAL,
    FLAP_MIN_PER_HOUR,
    FLAP_RATIO,
    HEARTBEAT_INTERVAL_S,
    RESTART_GAP_TOLERANCE_S,
    RETENTION_DAYS,
    RETENTION_INTERVAL_S,
    SIGNAL_WEAR_UPDATED,
    TRACKABLE_DOMAINS,
    WEAR_CRITICAL_DEBOUNCE_S,
    WEAR_CRITICAL_THRESHOLDS,
)
from .state_machine import EntityStateMachine, StateKind, derive_logical_state
from .storage import AsyncSqliteWriter

_LOG = logging.getLogger(__name__)

# Sensors refresh off this signal carrying the *meta id* (entity_meta.id), a
# surrogate key that survives entity_id renames — so a rename never strands a sensor.
_ON_STATE = str(StateKind.ON)


class WearCoordinator:
    def __init__(
        self,
        hass: HomeAssistant,
        writer: AsyncSqliteWriter,
        entry: ConfigEntry | None = None,
    ) -> None:
        self.hass = hass
        self.writer = writer
        self.entry = entry
        self.state_machine = EntityStateMachine()
        self._tracked: dict[str, registry.TrackedEntity] = {}
        self._source_device: dict[str, tuple[str, str] | None] = {}
        self._pending: set[asyncio.Task] = set()
        self._shutdown_started = False
        self._discovery_mode = DISCOVERY_PROMPT
        self._excluded_domains: set[str] = set()
        self._include_binary_sensors = False
        self._rates: dict[int, dict[str, float]] = {}
        self._health: dict[int, bool] = {}
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_heartbeat: Callable[[], None] | None = None
        self._unsub_registry: Callable[[], None] | None = None
        self._unsub_retention: Callable[[], None] | None = None

    async def async_start(self, entity_ids: list[str]) -> None:
        now_ts = int(time.time())
        if self.entry is not None:
            options = self.entry.options
            self._discovery_mode = options.get(CONF_DISCOVERY_MODE, DISCOVERY_PROMPT)
            self._excluded_domains = set(options.get(CONF_EXCLUDED_DOMAINS, []))
            self._include_binary_sensors = options.get(CONF_INCLUDE_BINARY_SENSORS, False)
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        for eid in entity_ids:
            state = self.hass.states.get(eid)
            domain = state.domain if state else eid.split(".", 1)[0]
            friendly = state.attributes.get("friendly_name") if state else None
            entry = ent_reg.async_get(eid)
            unique_id = entry.unique_id if entry else None
            device = self._source_device_entry(dev_reg, entry)
            manufacturer = device.manufacturer if device else None
            model = device.model if device else None
            self._source_device[eid] = (
                next(iter(device.identifiers)) if device and device.identifiers else None
            )
            rated = lookup_rated(manufacturer, model)
            rated_hours = rated.rated_hours if rated else None
            rated_cycles = rated.rated_cycles if rated else None
            tracked = await self.writer.run(
                lambda conn, eid=eid, domain=domain, friendly=friendly, unique_id=unique_id,
                manufacturer=manufacturer, model=model, rated_hours=rated_hours,
                rated_cycles=rated_cycles: registry.upsert(
                    conn,
                    entity_id=eid,
                    domain=domain,
                    friendly_name=friendly,
                    unique_id=unique_id,
                    manufacturer=manufacturer,
                    model=model,
                    rated_hours=rated_hours,
                    rated_cycles=rated_cycles,
                    tracking_since=now_ts,
                )
            )
            self._tracked[eid] = tracked

        last_alive = await self.writer.load_last_alive()
        mono = time.monotonic()
        for eid, tracked in self._tracked.items():
            state = self.hass.states.get(eid)
            logical = derive_logical_state(state)
            raw = state.state if state else None
            await self._recover_open_period(tracked, logical, now_ts, last_alive)
            ev = self.state_machine.observe(
                eid, logical, raw, now_ts, mono, tracked.debounce_s
            )
            if ev is not None:
                await self.writer.write_transition(tracked.id, ev)

        self._resubscribe_state()
        self._unsub_registry = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_registry_event
        )
        self._unsub_heartbeat = async_track_time_interval(
            self.hass,
            self._handle_heartbeat,
            timedelta(seconds=HEARTBEAT_INTERVAL_S),
        )
        self._unsub_retention = async_track_time_interval(
            self.hass,
            self._handle_retention,
            timedelta(seconds=RETENTION_INTERVAL_S),
        )
        await self._run_rollup(int(time.time()))

    @staticmethod
    def _source_device_entry(
        dev_reg: dr.DeviceRegistry, entry: er.RegistryEntry | None
    ) -> dr.DeviceEntry | None:
        if entry is None or entry.device_id is None:
            return None
        return dev_reg.async_get(entry.device_id)

    async def _recover_open_period(
        self,
        tracked: registry.TrackedEntity,
        logical: StateKind,
        now_ts: int,
        last_alive: int | None,
    ) -> None:
        """Credit a short downtime gap when a device was ON before the restart and
        is still ON now (DESIGN §4 — tolerate one missed heartbeat). Longer gaps are
        left uncredited rather than inventing on-time across an unknown downtime."""
        if logical is not StateKind.ON or last_alive is None:
            return
        summary = await self.writer.load_summary(tracked.id)
        if summary is None or summary.get("last_state") != _ON_STATE:
            return
        gap = now_ts - last_alive
        if 0 < gap <= RESTART_GAP_TOLERANCE_S:
            await self.writer.apply_accruals(
                [(tracked.id, float(gap), float(gap))], now_ts
            )
            _LOG.debug(
                "wear_tracker: recovered %ss of on-time for %s across restart",
                gap,
                tracked.entity_id,
            )

    @callback
    def _resubscribe_state(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
        self._unsub_state = async_track_state_change_event(
            self.hass, list(self._tracked.keys()), self._handle_state_change
        )

    @callback
    def _track_task(self, coro) -> None:
        task = self.hass.async_create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    @callback
    def _handle_state_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        entity_id: str = event.data["entity_id"]
        tracked = self._tracked.get(entity_id)
        if tracked is None or tracked.disabled:
            return

        logical = derive_logical_state(new_state)
        ts = int(new_state.last_updated.timestamp())
        mono = time.monotonic()
        ev = self.state_machine.observe(
            entity_id, logical, new_state.state, ts, mono, tracked.debounce_s
        )
        if ev is None:
            return

        self._track_task(self._persist_and_signal(tracked.id, entity_id, ev))

    async def _persist_and_signal(self, meta_id: int, entity_id: str, ev) -> None:
        try:
            await self.writer.write_transition(meta_id, ev)
        except Exception:
            _LOG.exception("wear_tracker: failed to persist transition for %s", entity_id)
            return
        async_dispatcher_send(self.hass, SIGNAL_WEAR_UPDATED, meta_id)

    def _collect_accruals(self, mono: float) -> list[tuple[int, float, float]]:
        accruals: list[tuple[int, float, float]] = []
        for delta in self.state_machine.flush(mono):
            tracked = self._tracked.get(delta.entity_id)
            if tracked is None or tracked.disabled:
                continue
            accruals.append(
                (tracked.id, delta.lifetime_seconds_delta, delta.connected_seconds_delta)
            )
        return accruals

    async def _handle_heartbeat(self, _now) -> None:
        ts = int(time.time())
        accruals = self._collect_accruals(time.monotonic())
        if accruals:
            try:
                await self.writer.apply_accruals(accruals, ts)
            except Exception:
                _LOG.exception("wear_tracker: heartbeat accrual flush failed")
        await self.writer.heartbeat(ts)
        await self._run_rollup(ts)

    async def _run_rollup(self, now_ts: int) -> None:
        """Recompute flap/connection rates, refresh health, fire debounced anomaly
        events, and signal sensors. Runs every heartbeat and once at startup."""
        for entity_id, tracked in list(self._tracked.items()):
            if tracked.disabled:
                continue
            meta_id = tracked.id
            try:
                result = await self.writer.run(
                    lambda conn, mid=meta_id, rh=tracked.rated_hours: rollup.evaluate(
                        conn,
                        mid,
                        now_ts,
                        window_days=BASELINE_WINDOW_DAYS,
                        refresh_s=BASELINE_REFRESH_S,
                        floor=BASELINE_FLOOR,
                        flap_ratio=FLAP_RATIO,
                        flap_min=FLAP_MIN_PER_HOUR,
                        conn_ratio=CONNECTION_RATIO,
                        conn_min=CONNECTION_MIN_PER_HOUR,
                        debounce_s=ANOMALY_DEBOUNCE_S,
                        rated_hours=rh,
                        wear_thresholds=WEAR_CRITICAL_THRESHOLDS,
                        wear_debounce_s=WEAR_CRITICAL_DEBOUNCE_S,
                    )
                )
            except Exception:
                _LOG.exception("wear_tracker: rollup failed for %s", entity_id)
                continue
            self._rates[meta_id] = result["rates"]
            self._health[meta_id] = result["health"]
            if result["flap"]["due"]:
                events.fire_flap_anomaly(
                    self.hass, entity_id, result["flap"]["rate"], result["flap"]["baseline"]
                )
            if result["connection"]["due"]:
                events.fire_connection_anomaly(
                    self.hass,
                    entity_id,
                    result["connection"]["rate"],
                    result["connection"]["baseline"],
                )
            for crossing in result["wear_critical"]:
                self.hass.bus.async_fire(
                    EVENT_WEAR_CRITICAL,
                    {
                        "entity_id": entity_id,
                        "metric": "hours",
                        "pct": crossing["threshold"],
                        "value": crossing["value"],
                        "rated": crossing["rated"],
                    },
                )
            async_dispatcher_send(self.hass, SIGNAL_WEAR_UPDATED, meta_id)

    async def _handle_retention(self, _now) -> None:
        cutoff = int(time.time()) - RETENTION_DAYS * 86400
        try:
            purged = await self.writer.run(
                lambda conn: storage.fold_and_purge_sync(conn, cutoff, DEFAULT_DEBOUNCE_S)
            )
        except Exception:
            _LOG.exception("wear_tracker: retention fold/purge failed")
            return
        if purged:
            _LOG.info("wear_tracker: folded and purged %d transitions older than 90d", purged)

    @callback
    def _handle_registry_event(self, event: Event) -> None:
        self._track_task(self._async_handle_registry_event(event))

    async def _async_handle_registry_event(self, event: Event) -> None:
        action = event.data.get("action")
        entity_id = event.data.get("entity_id")
        if action == "create":
            await self._handle_entity_created(entity_id)
        elif action == "remove":
            if entity_id in self._tracked:
                await self._handle_entity_removed(entity_id)
        elif action == "update":
            old_entity_id = event.data.get("old_entity_id")
            if old_entity_id and entity_id and old_entity_id != entity_id and old_entity_id in self._tracked:
                await self._handle_entity_renamed(old_entity_id, entity_id)

    async def _handle_entity_created(self, entity_id: str | None) -> None:
        if not entity_id or entity_id in self._tracked:
            return
        domain = entity_id.split(".", 1)[0]
        if domain not in TRACKABLE_DOMAINS or domain in self._excluded_domains:
            return
        if domain == "binary_sensor" and not self._include_binary_sensors:
            return
        if self._discovery_mode == DISCOVERY_AUTO_TRACK:
            await self.async_add_tracked_entities([entity_id])
        elif self._discovery_mode == DISCOVERY_PROMPT:
            repairs.async_create_new_device_issue(self.hass, entity_id, domain)

    async def async_add_tracked_entities(self, new_entity_ids: list[str]) -> None:
        """Add entities to the config entry and reload (the update listener does
        the reload, which re-resolves and re-creates sensors)."""
        if self.entry is None:
            return
        current = list(self.entry.options.get(CONF_ENTITIES, []))
        added = [e for e in new_entity_ids if e not in current]
        if not added:
            return
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_ENTITIES: current + added}
        )

    async def _handle_entity_renamed(self, old_entity_id: str, new_entity_id: str) -> None:
        if old_entity_id not in self._tracked:
            return
        entry = er.async_get(self.hass).async_get(new_entity_id)
        target_uid = entry.unique_id if entry else None
        moves = await self.writer.run(
            lambda conn: registry.reconcile_rename(
                conn, old_entity_id, new_entity_id, target_uid
            )
        )
        if not moves:
            return
        for src, dst in moves:
            await self._apply_move(src, dst)
        self._resubscribe_state()
        _LOG.info("wear_tracker: entity renamed %s -> %s", old_entity_id, new_entity_id)

    async def _apply_move(self, src: str, dst: str) -> None:
        """Re-key one entity_meta move (including sentinel parks) in memory."""
        if src not in self._tracked:
            return
        del self._tracked[src]
        refreshed = await self.writer.run(
            lambda conn: registry.load_by_entity_id(conn, dst)
        )
        if refreshed is not None:
            self._tracked[dst] = refreshed
        self.state_machine.rename_key(src, dst)
        self._source_device[dst] = self._source_device.pop(src, None)

    async def _handle_entity_removed(self, entity_id: str | None) -> None:
        if entity_id is None:
            return
        tracked = self._tracked.pop(entity_id, None)
        if tracked is None:
            return
        await self.writer.run(
            lambda conn: registry.set_disabled(conn, entity_id, True)
        )
        self.state_machine.reset(entity_id)
        self._source_device.pop(entity_id, None)
        self._resubscribe_state()
        _LOG.info(
            "wear_tracker: %s removed from registry; tracking paused (history kept)",
            entity_id,
        )

    async def async_shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        for unsub in (
            self._unsub_state,
            self._unsub_heartbeat,
            self._unsub_registry,
            self._unsub_retention,
        ):
            if unsub is not None:
                unsub()
        self._unsub_state = self._unsub_heartbeat = None
        self._unsub_registry = self._unsub_retention = None
        ts = int(time.time())
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
        try:
            accruals = self._collect_accruals(time.monotonic())
            if accruals:
                await self.writer.apply_accruals(accruals, ts)
        except Exception:
            _LOG.exception("wear_tracker: shutdown accrual flush failed")
        try:
            await self.writer.heartbeat(ts)
        except Exception:
            _LOG.exception("wear_tracker: final heartbeat failed")
        finally:
            await self.writer.close()

    async def async_get_summary(self, meta_id: int) -> dict[str, Any] | None:
        # A sensor refresh can be in flight when the writer closes on shutdown;
        # don't let that surface as a task exception.
        if self._shutdown_started:
            return None
        try:
            return await self.writer.load_summary(meta_id)
        except RuntimeError:
            return None

    def get_tracked(self, entity_id: str) -> registry.TrackedEntity | None:
        return self._tracked.get(entity_id)

    def get_source_device(self, entity_id: str) -> tuple[str, str] | None:
        return self._source_device.get(entity_id)

    def get_rates(self, meta_id: int) -> dict[str, float] | None:
        return self._rates.get(meta_id)

    def get_health(self, meta_id: int) -> bool:
        return self._health.get(meta_id, False)

    def tracked_entity_ids(self) -> list[str]:
        return list(self._tracked.keys())
