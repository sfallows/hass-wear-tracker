"""Admin services (DESIGN §9): reset, set_rated, export_log, recompute, disable,
purge, purge_all. All admin-only; every mutation writes an audit_log row."""
from __future__ import annotations

import csv
import json
import logging
import time
from pathlib import Path

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from . import registry, storage
from .const import (
    CONF_DISCOVERY_MODE,
    CONF_ENTITIES,
    CONF_EXCLUDED_ENTITIES,
    DB_SUBDIR,
    DEFAULT_DEBOUNCE_S,
    DISCOVERY_AUTO_TRACK,
    DOMAIN,
    SIGNAL_WEAR_UPDATED,
)

_LOG = logging.getLogger(__name__)

SERVICE_RESET = "reset"
SERVICE_SET_RATED = "set_rated"
SERVICE_EXPORT_LOG = "export_log"
SERVICE_RECOMPUTE = "recompute"
SERVICE_DISABLE = "disable"
SERVICE_PURGE = "purge"
SERVICE_PURGE_ALL = "purge_all"

_ALL_SERVICES = (
    SERVICE_RESET,
    SERVICE_SET_RATED,
    SERVICE_EXPORT_LOG,
    SERVICE_RECOMPUTE,
    SERVICE_DISABLE,
    SERVICE_PURGE,
    SERVICE_PURGE_ALL,
)


def _coordinator(hass: HomeAssistant):
    return next(iter((hass.data.get(DOMAIN) or {}).values()), None)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_RESET):
        return

    async def _meta_id(coordinator, entity_id: str) -> int:
        tracked = coordinator.get_tracked(entity_id)
        if tracked is not None:
            return tracked.id
        row = await coordinator.writer.run(
            lambda conn: registry.load_by_entity_id(conn, entity_id)
        )
        if row is None:
            raise ServiceValidationError(f"{entity_id} is not tracked by wear_tracker")
        return row.id

    def _require_coordinator():
        coordinator = _coordinator(hass)
        if coordinator is None:
            raise ServiceValidationError("Wear Tracker is not set up")
        return coordinator

    async def _audit(coordinator, meta_id, action, payload, ts):
        await coordinator.writer.run(
            lambda conn: storage.write_audit_sync(conn, meta_id, action, json.dumps(payload), ts)
        )

    async def _reload(coordinator) -> None:
        if coordinator.entry is not None:
            await hass.config_entries.async_reload(coordinator.entry.entry_id)

    def _untrack_in_options(coordinator, entity_id: str, *, exclude: bool) -> bool:
        """Drop entity_id from the entry's tracked list so a reload can't re-track
        it. When `exclude` (auto_track mode), also add it to CONF_EXCLUDED_ENTITIES
        — otherwise scan_trackable would re-discover a still-existing entity on the
        post-purge reload and undo the purge. Returns True when the entry changed
        (which reloads via its update listener); False means the caller reloads."""
        entry = coordinator.entry
        if entry is None:
            return False
        entities = list(entry.options.get(CONF_ENTITIES, []))
        excluded = list(entry.options.get(CONF_EXCLUDED_ENTITIES, []))
        changed = False
        if entity_id in entities:
            entities.remove(entity_id)
            changed = True
        if exclude and entity_id not in excluded:
            excluded.append(entity_id)
            changed = True
        if not changed:
            return False
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_ENTITIES: entities,
                CONF_EXCLUDED_ENTITIES: excluded,
            },
        )
        return True

    async def handle_reset(call: ServiceCall) -> None:
        coordinator = _require_coordinator()
        entity_id = call.data["entity_id"]
        keep = call.data["keep_history"]
        meta_id = await _meta_id(coordinator, entity_id)
        ts = int(time.time())
        prior = await coordinator.writer.run(
            lambda conn: storage.reset_summary_sync(conn, meta_id, keep, ts)
        )
        await _audit(coordinator, meta_id, "reset",
                     {"entity_id": entity_id, "keep_history": keep, "prior": prior}, ts)
        await _clear_lts(hass, coordinator, entity_id)
        await _reload(coordinator)

    async def handle_set_rated(call: ServiceCall) -> None:
        coordinator = _require_coordinator()
        entity_id = call.data["entity_id"]
        hours = call.data.get("hours")
        cycles = call.data.get("cycles")
        if hours is None and cycles is None:
            raise ServiceValidationError("Provide hours and/or cycles")
        meta_id = await _meta_id(coordinator, entity_id)
        ts = int(time.time())
        prior = await coordinator.writer.run(
            lambda conn: registry.load_by_entity_id(conn, entity_id)
        )
        await coordinator.writer.run(
            lambda conn: storage.set_rated_sync(conn, meta_id, hours, cycles)
        )
        await _audit(coordinator, meta_id, "set_rated", {
            "entity_id": entity_id,
            "hours": hours,
            "cycles": cycles,
            "prior": {"rated_hours": prior.rated_hours if prior else None,
                      "rated_cycles": prior.rated_cycles if prior else None},
        }, ts)
        await _reload(coordinator)

    async def handle_disable(call: ServiceCall) -> None:
        coordinator = _require_coordinator()
        entity_id = call.data["entity_id"]
        disabled = call.data["disabled"]
        meta_id = await _meta_id(coordinator, entity_id)
        ts = int(time.time())
        await coordinator.writer.run(
            lambda conn: registry.set_disabled(conn, entity_id, disabled, "user")
        )
        await _audit(coordinator, meta_id, "disable",
                     {"entity_id": entity_id, "disabled": disabled}, ts)
        await _reload(coordinator)

    async def handle_purge(call: ServiceCall) -> None:
        coordinator = _require_coordinator()
        entity_id = call.data["entity_id"]
        meta_id = await _meta_id(coordinator, entity_id)
        ts = int(time.time())
        row = await coordinator.writer.run(
            lambda conn: registry.load_by_entity_id(conn, entity_id)
        )
        # Audit BEFORE delete, capturing identity for the post-purge record.
        await _audit(coordinator, None, "purge", {
            "entity_id": entity_id,
            "unique_id": row.unique_id if row else None,
        }, ts)
        await coordinator.writer.run(lambda conn: storage.purge_entity_sync(conn, meta_id))
        # DESIGN §9: purge is irreversible. Remove the entity from the entry's
        # tracked options so the reload doesn't re-upsert a fresh zeroed row and
        # resurrect the device; in auto_track also exclude it so scan_trackable
        # can't re-discover a still-existing entity. Updating options already
        # reloads via the update listener; only reload explicitly when unchanged.
        auto_track = (
            coordinator.entry is not None
            and coordinator.entry.options.get(CONF_DISCOVERY_MODE) == DISCOVERY_AUTO_TRACK
        )
        if not _untrack_in_options(coordinator, entity_id, exclude=auto_track):
            await _reload(coordinator)

    async def handle_purge_all(call: ServiceCall) -> None:
        coordinator = _require_coordinator()
        if not call.data["confirm"]:
            raise ServiceValidationError("purge_all requires confirm: true")
        ts = int(time.time())
        purged = await coordinator.writer.run(storage.purge_all_sync)
        await _audit(coordinator, None, "purge_all", {"purged": purged}, ts)
        await _reload(coordinator)
        return {"purged": purged}

    async def handle_recompute(call: ServiceCall) -> ServiceResponse:
        coordinator = _require_coordinator()
        entity_id = call.data.get("entity_id")
        targets = [entity_id] if entity_id else coordinator.tracked_entity_ids()
        ts = int(time.time())
        recomputed = []
        for eid in targets:
            try:
                meta_id = await _meta_id(coordinator, eid)
            except ServiceValidationError:
                continue
            await coordinator.writer.run(
                lambda conn, mid=meta_id: storage.recompute_summary_sync(conn, mid, DEFAULT_DEBOUNCE_S)
            )
            await _audit(coordinator, meta_id, "recompute", {"entity_id": eid}, ts)
            async_dispatcher_send(hass, SIGNAL_WEAR_UPDATED, meta_id)
            recomputed.append(eid)
        return {"recomputed": recomputed}

    async def handle_export_log(call: ServiceCall) -> ServiceResponse:
        coordinator = _require_coordinator()
        entity_id = call.data["entity_id"]
        meta_id = await _meta_id(coordinator, entity_id)
        start_ts = int(dt_util.as_timestamp(call.data["start"]))
        end_ts = int(dt_util.as_timestamp(call.data["end"]))
        filename = call.data.get("filename") or f"{entity_id}_{start_ts}.csv"
        _validate_filename(filename)
        rows = await coordinator.writer.run(
            lambda conn: storage.export_rows_sync(conn, meta_id, start_ts, end_ts)
        )
        export_dir = Path(hass.config.path(DB_SUBDIR)) / "exports"
        path = export_dir / filename
        if not path.resolve().is_relative_to(export_dir.resolve()):
            raise ServiceValidationError("filename escapes the exports directory")

        def _write() -> None:
            export_dir.mkdir(parents=True, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["ts", "from", "to", "raw_from", "raw_to", "delta_s"])
                for row in rows:
                    writer.writerow([row["ts"], row["from_state"], row["to_state"],
                                     row["raw_from"], row["raw_to"], row["delta_s"]])

        await hass.async_add_executor_job(_write)
        return {"path": str(path), "rows": len(rows)}

    entity_schema = vol.Schema({vol.Required("entity_id"): cv.entity_id})
    async_register_admin_service(
        hass, DOMAIN, SERVICE_RESET, handle_reset,
        entity_schema.extend({vol.Optional("keep_history", default=False): cv.boolean}),
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_SET_RATED, handle_set_rated,
        entity_schema.extend({
            vol.Optional("hours"): vol.Coerce(float),
            vol.Optional("cycles"): vol.Coerce(int),
        }),
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_DISABLE, handle_disable,
        entity_schema.extend({vol.Optional("disabled", default=True): cv.boolean}),
    )
    async_register_admin_service(hass, DOMAIN, SERVICE_PURGE, handle_purge, entity_schema)
    async_register_admin_service(
        hass, DOMAIN, SERVICE_PURGE_ALL, handle_purge_all,
        vol.Schema({vol.Required("confirm", default=False): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_RECOMPUTE, handle_recompute,
        vol.Schema({vol.Optional("entity_id"): cv.entity_id}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_EXPORT_LOG, handle_export_log,
        entity_schema.extend({
            vol.Required("start"): cv.datetime,
            vol.Required("end"): cv.datetime,
            vol.Optional("filename"): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    for service in _ALL_SERVICES:
        hass.services.async_remove(DOMAIN, service)


def _validate_filename(filename: str) -> None:
    if (
        "/" in filename
        or "\\" in filename
        or ".." in filename
        or filename.startswith(".")
        or not filename.endswith(".csv")
    ):
        raise ServiceValidationError(
            "filename must be a bare *.csv name (no path separators or '..')"
        )


async def _clear_lts(hass: HomeAssistant, coordinator, entity_id: str) -> None:
    """Best-effort clear of our sensors' long-term statistics (undocumented API)."""
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.helpers import entity_registry as er

        from .sensor import WearPercent, _SENSOR_CLASSES

        tracked = coordinator.get_tracked(entity_id)
        if tracked is None:
            return
        unique_root = registry.wear_sensor_root(tracked)
        # Match this entity's own sensor unique_ids exactly. A startswith(prefix)
        # scan would also match a sibling like switch.pump_2 when resetting
        # switch.pump (HA's duplicate-naming convention) and wipe its statistics.
        sensor_keys = {cls.SENSOR_KEY for cls in _SENSOR_CLASSES}
        sensor_keys |= {WearPercent.SENSOR_KEY, "health_alert"}
        own_ids = {f"wear_tracker_{unique_root}_{key}" for key in sensor_keys}
        stat_ids = [
            e.entity_id
            for e in er.async_get(hass).entities.values()
            if e.platform == DOMAIN and e.unique_id in own_ids
        ]
        if stat_ids:
            get_instance(hass).async_clear_statistics(stat_ids)
    except Exception:  # pragma: no cover - defensive; recorder may be absent
        _LOG.debug("wear_tracker: could not clear LTS for %s", entity_id, exc_info=True)
