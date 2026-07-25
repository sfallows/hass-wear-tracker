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
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started

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
    CONF_EXCLUDED_ENTITIES,
    CONF_INCLUDE_BINARY_SENSORS,
    CONNECTION_MIN_PER_HOUR,
    CONNECTION_RATIO,
    DEFAULT_DEBOUNCE_S,
    DISCOVERY_AUTO_TRACK,
    DISCOVERY_PROMPT,
    DOMAIN,
    EVENT_WEAR_CRITICAL,
    FLAP_MIN_PER_HOUR,
    FLAP_RATIO,
    HEARTBEAT_INTERVAL_S,
    RESTART_CREDIT_MAX_S,
    RESTART_GAP_TOLERANCE_S,
    RETENTION_DAYS,
    RETENTION_INTERVAL_S,
    SEED_RETRY_INITIAL_S,
    SEED_RETRY_MAX_S,
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
        # Set once the deferred seed has installed the state subscription; before
        # then registry handlers must not install it (the seed will).
        self._seeded = False
        # True once the deferred seed loop has finished. While False, a live event
        # that bootstraps a machine records its ts in _boot_observed so the seed can
        # end that entity's restart credit at the bootstrap (no overlap with the live
        # accrual that starts there) instead of skipping the credit entirely.
        self._seed_complete = False
        self._boot_observed: dict[str, int] = {}
        # Captured at entry setup (before the slow boot-to-started delay) so the
        # restart-gap tolerance is measured against the true offline gap.
        self._setup_ts = 0
        self._seed_retry_delay = SEED_RETRY_INITIAL_S
        self._discovery_mode = DISCOVERY_PROMPT
        self._excluded_domains: set[str] = set()
        self._excluded_entities: set[str] = set()
        self._include_binary_sensors = False
        self._rates: dict[int, dict[str, float]] = {}
        self._health: dict[int, bool] = {}
        # Accruals whose persist failed on a prior tick; retried next tick so a
        # transient write error can't permanently drop already-flushed on-time.
        self._pending_accruals: list[tuple[int, float, float]] = []
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_heartbeat: Callable[[], None] | None = None
        self._unsub_registry: Callable[[], None] | None = None
        self._unsub_retention: Callable[[], None] | None = None
        self._unsub_started: Callable[[], None] | None = None
        self._unsub_seed_retry: Callable[[], None] | None = None

    async def async_start(self, entity_ids: list[str]) -> None:
        now_ts = int(time.time())
        self._setup_ts = now_ts
        if self.entry is not None:
            options = self.entry.options
            self._discovery_mode = options.get(CONF_DISCOVERY_MODE, DISCOVERY_PROMPT)
            self._excluded_domains = set(options.get(CONF_EXCLUDED_DOMAINS, []))
            self._excluded_entities = set(options.get(CONF_EXCLUDED_ENTITIES, []))
            self._include_binary_sensors = options.get(CONF_INCLUDE_BINARY_SENSORS, False)
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        # The legacy NULL-platform fallback guard (registry.upsert) may only be
        # blocked by an entity_id that belongs to a LIVE entity. A stale, dead-ghost
        # id still lingering in CONF_ENTITIES must not refuse the fallback — that
        # reintroduces the downtime-rename history fork (F6). "Live" = an entity
        # registry entry present or a current state present.
        known_ids = {
            eid
            for eid in entity_ids
            if ent_reg.async_get(eid) is not None
            or self.hass.states.get(eid) is not None
        }

        for eid in entity_ids:
            state = self.hass.states.get(eid)
            domain = state.domain if state else eid.split(".", 1)[0]
            friendly = state.attributes.get("friendly_name") if state else None
            entry = ent_reg.async_get(eid)
            unique_id = entry.unique_id if entry else None
            platform = entry.platform if entry else None
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
                platform=platform, manufacturer=manufacturer, model=model,
                rated_hours=rated_hours, rated_cycles=rated_cycles: registry.upsert(
                    conn,
                    entity_id=eid,
                    domain=domain,
                    friendly_name=friendly,
                    unique_id=unique_id,
                    platform=platform,
                    manufacturer=manufacturer,
                    model=model,
                    rated_hours=rated_hours,
                    rated_cycles=rated_cycles,
                    tracking_since=now_ts,
                    known_entity_ids=known_ids,
                )
            )
            self._tracked[eid] = tracked

        self._unsub_registry = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_registry_event
        )
        self._unsub_retention = async_track_time_interval(
            self.hass,
            self._handle_retention,
            timedelta(seconds=RETENTION_INTERVAL_S),
        )
        # Seeding must observe real source states; at boot those load *after* us,
        # so gate on HA-started (fires immediately if already running). Seeding a
        # machine DISCONNECTED here would fabricate a cycle when the real ON state
        # arrives, and rob _recover_open_period of its restart on-time credit.
        self._unsub_started = async_at_started(self.hass, self._schedule_seed)

    @callback
    def _schedule_seed(self, _hass: HomeAssistant) -> None:
        # Run seeding as a tracked task so async_shutdown awaits it before closing
        # the writer (it fires as an eager task that could otherwise outlive it).
        self._track_task(self._async_seed_on_started())

    async def _async_seed_on_started(self) -> None:
        if self._shutdown_started:
            return
        try:
            await self._seed_once()
        except Exception:
            # A transient boot-time DB error (locked, disk full) would otherwise
            # leave the integration frozen with no subscriptions and no retry; back
            # off and try again instead of dying in this fire-and-forget task.
            if self._shutdown_started:
                return
            delay = self._seed_retry_delay
            if delay == SEED_RETRY_INITIAL_S:
                _LOG.warning(
                    "wear_tracker: seeding failed; retrying in %ss", delay, exc_info=True
                )
            else:
                _LOG.debug("wear_tracker: seeding retry failed; retrying in %ss", delay)
            self._seed_retry_delay = min(delay * 2, SEED_RETRY_MAX_S)
            self._unsub_seed_retry = async_call_later(
                self.hass, delay, self._seed_retry_fired
            )

    @callback
    def _seed_retry_fired(self, _now) -> None:
        self._unsub_seed_retry = None
        if self._shutdown_started:
            return
        self._track_task(self._async_seed_on_started())

    async def _seed_once(self) -> None:
        # Install the state subscription first so state changes fired right at
        # HA-started (startup automations) aren't dropped; observe()'s bootstrap
        # handles events for entities the seed loop hasn't reached yet, and the loop
        # skips any machine a live event already created so it isn't double-observed.
        # async_shutdown unsubscribes this if it interleaves during the awaits below.
        self._resubscribe_state()
        self._seeded = True
        now_ts = int(time.time())
        last_alive = await self.writer.load_last_alive()
        mono = time.monotonic()
        # Re-resolve tracked per iteration (not a stale pre-await snapshot): a rename
        # or removal mid-seed pops/renames the key, and processing the dead key would
        # fabricate a ghost DISCONNECTED machine and a bogus transition on the moved
        # row's meta id. Skip disabled rows too (every live accrual path does).
        for eid in list(self._tracked):
            tracked = self._tracked.get(eid)
            if tracked is None or tracked.disabled:
                continue
            state = self.hass.states.get(eid)
            logical = derive_logical_state(state)
            raw = state.state if state else None
            boot_ts = self._boot_observed.get(eid)
            if boot_ts is not None:
                # A live event already bootstrapped this machine mid-seed. Still apply
                # the restart credit, but end it at the bootstrap ts so it doesn't
                # overlap the live accrual that began there; don't observe again
                # (that would double-record the bootstrap).
                await self._recover_open_period(
                    tracked, logical, now_ts, last_alive, credit_end=boot_ts
                )
                continue
            if eid in self.state_machine.snapshot():
                continue
            await self._recover_open_period(tracked, logical, now_ts, last_alive)
            if eid in self.state_machine.snapshot():
                continue
            ev = self.state_machine.observe(
                eid, logical, raw, now_ts, mono, tracked.debounce_s
            )
            if ev is not None:
                await self.writer.write_transition(tracked.id, ev)

        self._seed_complete = True
        self._boot_observed.clear()

        # Stamp a fresh heartbeat now that the restart-gap credit is applied, so a
        # second restart within the tolerance window recomputes the gap from this
        # boot rather than re-crediting the same downtime off the stale last_alive.
        await self.writer.heartbeat(now_ts)
        # async_shutdown may have run its unsub pass during the awaits above;
        # installing the heartbeat timer after it would leak it.
        if self._shutdown_started:
            return
        self._unsub_heartbeat = async_track_time_interval(
            self.hass,
            self._handle_heartbeat,
            timedelta(seconds=HEARTBEAT_INTERVAL_S),
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
        credit_end: int | None = None,
    ) -> None:
        """Credit a short downtime gap when a device was ON before the restart and
        is still ON now (DESIGN §4 — tolerate one missed heartbeat). Longer gaps are
        left uncredited rather than inventing on-time across an unknown downtime.

        `credit_end` bounds the credited window's end (default seed-now); when a live
        event bootstrapped the machine mid-seed it is the bootstrap ts, so the credit
        stops where live accrual begins."""
        if logical is not StateKind.ON or last_alive is None:
            return
        summary = await self.writer.load_summary(tracked.id)
        if summary is None or summary.get("last_state") != _ON_STATE:
            return
        # Gate on the true offline gap measured at entry setup, not at seed-now:
        # boot-to-started can be 100-150s on slow hosts, and measuring the gap here
        # would blow the tolerance so the credit that matters most silently never
        # applies. When the outage was short, credit the stayed-ON window
        # (last_alive -> credit end), capped so a slow boot or seed-retry backoff
        # can't fabricate many minutes if the device was toggled while unobserved.
        offline_gap = self._setup_ts - last_alive
        end = now_ts if credit_end is None else credit_end
        if 0 < offline_gap <= RESTART_GAP_TOLERANCE_S:
            credit = min(end - last_alive, RESTART_CREDIT_MAX_S)
            if credit > 0:
                await self.writer.apply_accruals(
                    [(tracked.id, float(credit), float(credit))], now_ts
                )
                _LOG.debug(
                    "wear_tracker: recovered %ss of on-time for %s across restart",
                    credit,
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
    def _observe_current_state(self, entity_id: str) -> None:
        """Re-observe an entity's current real state after a rename so the machine
        heals whatever the old id's state-removal / reconcile ordering was (the new
        id's live change may have fired before it was tracked). Only heal from a
        *present* state: if the new state hasn't landed yet, fabricating DISCONNECTED
        here would record the very spurious drop the rename handling avoids — the
        live subscription (now covering the new id) delivers the real state."""
        tracked = self._tracked.get(entity_id)
        if tracked is None or tracked.disabled:
            return
        state = self.hass.states.get(entity_id)
        if state is None:
            return
        ev = self.state_machine.observe(
            entity_id,
            derive_logical_state(state),
            state.state,
            int(state.last_updated.timestamp()),
            time.monotonic(),
            tracked.debounce_s,
        )
        if ev is not None:
            self._track_task(self._persist_and_signal(tracked.id, entity_id, ev))

    @callback
    def _track_task(self, coro) -> None:
        task = self.hass.async_create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    @callback
    def _handle_state_change(self, event: Event) -> None:
        entity_id: str = event.data["entity_id"]
        tracked = self._tracked.get(entity_id)
        if tracked is None or tracked.disabled:
            return

        new_state = event.data.get("new_state")
        logical = derive_logical_state(new_state)
        if new_state is not None:
            ts = int(new_state.last_updated.timestamp())
            raw = new_state.state
        else:
            # new_state=None can mean the source integration dropped the state
            # (reload/disable) OR a rename that removes the old entity_id before the
            # coordinator's reconcile task runs OR a full removal. Only the first is
            # a real disconnect; the others must not park the machine DISCONNECTED
            # while the device is on (that halts accrual until the next real toggle).
            ent_reg = er.async_get(self.hass)
            if ent_reg.async_get(entity_id) is not None:
                # ER entry for this entity_id survives → source dropped the state.
                # Observe DISCONNECTED so accrual stops (deliberate reload/disable fix).
                ts = int(time.time())
                raw = None
            else:
                # ER entry for this entity_id is gone. If this identity now resolves to
                # a DIFFERENT entity_id a rename is in flight (the reconcile will move
                # the machine and re-observe) — skip, don't record a spurious drop.
                # Otherwise it's a genuine removal (the registry removed-handler also
                # observes DISCONNECTED; a same-state observe is a noop) or a legacy
                # entity never in the ER: observe DISCONNECTED so a real state-drop is
                # never skipped forever — an ER entry that vanished without a
                # coordinator-visible event would otherwise leave a phantom ON.
                moved_to = None
                if tracked.platform is not None and tracked.unique_id is not None:
                    moved_to = ent_reg.async_get_entity_id(
                        tracked.domain, tracked.platform, tracked.unique_id
                    )
                if moved_to is not None and moved_to != entity_id:
                    return
                ts = int(time.time())
                raw = None
        mono = time.monotonic()
        seeding = not self._seed_complete
        bootstrap = seeding and entity_id not in self.state_machine.snapshot()
        ev = self.state_machine.observe(
            entity_id, logical, raw, ts, mono, tracked.debounce_s
        )
        if ev is None:
            return
        if bootstrap:
            # Mid-seed live bootstrap: remember the observation ts so the seed loop
            # ends this entity's restart credit here instead of overlapping accrual.
            self._boot_observed[entity_id] = ev.ts

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
        # flush() has already advanced the machines' credit anchors, so include
        # any deltas a prior tick failed to persist — otherwise they're lost.
        accruals = self._pending_accruals + self._collect_accruals(time.monotonic())
        self._pending_accruals = []
        if accruals:
            try:
                await self.writer.apply_accruals(accruals, ts)
            except Exception:
                _LOG.exception("wear_tracker: heartbeat accrual flush failed")
                self._pending_accruals = accruals
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
                    lambda conn, mid=meta_id, rh=tracked.rated_hours,
                    rc=tracked.rated_cycles: rollup.evaluate(
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
                        rated_cycles=rc,
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
                        "metric": crossing["metric"],
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
        if not entity_id:
            return
        # A disabled tracked entry counts as untracked here: after a restart between
        # removal and re-pair, async_start puts the removal-disabled row back into
        # _tracked, so a plain `entity_id in self._tracked` guard would swallow the
        # re-pair and leave tracking frozen. Fall through so the re-registration
        # schedules a reload (which resumes via the identity path).
        existing = self._tracked.get(entity_id)
        if existing is not None and not existing.disabled:
            return
        if entity_id in self._excluded_entities:
            return
        domain = entity_id.split(".", 1)[0]
        if domain in self._excluded_domains:
            return
        # Never track our own entities (e.g. binary_sensor.*_health_alert): doing
        # so would spawn sensors that re-trigger discovery in an unbounded loop.
        reg_entry = er.async_get(self.hass).async_get(entity_id)
        if reg_entry is not None and reg_entry.platform == DOMAIN:
            return
        # A configured entity that isn't currently tracked was live-removed (its row
        # soft-disabled and popped) and is now re-created for the same entity_id.
        # async_add_tracked_entities can't re-add it (already in CONF_ENTITIES), so
        # schedule a reload to re-run the upsert/resume path — an identity match
        # resumes counting without a full HA restart, in every discovery mode.
        if self.entry is not None and entity_id in self.entry.options.get(
            CONF_ENTITIES, []
        ):
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            return
        if domain not in TRACKABLE_DOMAINS:
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
        target_platform = entry.platform if entry else None
        target_domain = entry.domain if entry else new_entity_id.split(".", 1)[0]
        moves = await self.writer.run(
            lambda conn: registry.reconcile_rename(
                conn, old_entity_id, new_entity_id, target_uid,
                target_platform, target_domain,
            )
        )
        if not moves:
            return
        for src, dst in moves:
            await self._apply_move(src, dst)
        # Pre-seed registry churn must not install the state subscription on unseeded
        # machines (that fabricates cycles/drops from pre-started states); the seed
        # installs it. After seeding, re-observe the new id's real state so the
        # machine heals whatever the old id's state-removal / reconcile ordering was.
        if self._seeded:
            self._resubscribe_state()
            self._observe_current_state(new_entity_id)
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
        # Close the final on/connected period and record the drop BEFORE tearing the
        # machine down, so last_state lands DISCONNECTED and the last ON period isn't
        # lost. If _handle_state_change's None-branch already observed DISCONNECTED
        # (either event order is possible), this same-state observe returns None, so
        # there is no double-record.
        ev = self.state_machine.observe(
            entity_id, StateKind.DISCONNECTED, None, int(time.time()),
            time.monotonic(), tracked.debounce_s,
        )
        if ev is not None:
            await self.writer.write_transition(tracked.id, ev)
        await self.writer.run(
            lambda conn: registry.set_disabled(conn, entity_id, True, "removed")
        )
        self.state_machine.reset(entity_id)
        self._source_device.pop(entity_id, None)
        # Only refresh the live subscription once the seed has installed it; before
        # the seed runs it will pick up the post-churn tracked set itself.
        if self._seeded:
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
            self._unsub_started,
            self._unsub_seed_retry,
        ):
            if unsub is not None:
                unsub()
        self._unsub_state = self._unsub_heartbeat = None
        self._unsub_registry = self._unsub_retention = None
        self._unsub_started = self._unsub_seed_retry = None
        ts = int(time.time())
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
        try:
            accruals = self._pending_accruals + self._collect_accruals(time.monotonic())
            self._pending_accruals = []
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
