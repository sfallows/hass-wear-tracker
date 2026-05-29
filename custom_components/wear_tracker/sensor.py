"""Sensor platform for wear_tracker.

Five v0.1 sensors per tracked entity (DESIGN.md §6):
lifetime_hours / connected_hours / lifetime_cycles / connection_drops / duty_cycle_pct.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_WEAR_UPDATED
from .coordinator import WearCoordinator
from .registry import TrackedEntity

_LOG = logging.getLogger(__name__)


class _WearBaseSensor(SensorEntity):
    """Base for wear-tracker sensors."""

    SENSOR_KEY: str = ""
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: WearCoordinator,
        tracked: TrackedEntity,
        via_device: tuple[str, str] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._tracked = tracked
        self._meta_id = tracked.id
        self._summary: dict[str, Any] | None = None
        unique_root = tracked.unique_id or tracked.entity_id
        self._attr_unique_id = f"wear_tracker_{unique_root}_{self.SENSOR_KEY}"
        device_info = DeviceInfo(
            identifiers={(DOMAIN, str(tracked.id))},
            name=tracked.friendly_name or tracked.entity_id,
            manufacturer=tracked.manufacturer,
            model=tracked.model,
        )
        if via_device is not None:
            device_info["via_device"] = via_device
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_WEAR_UPDATED, self._on_signal
            )
        )
        await self._refresh()

    @callback
    def _on_signal(self, meta_id: int) -> None:
        if meta_id != self._meta_id:
            return
        self.hass.async_create_task(self._refresh())

    async def _refresh(self) -> None:
        self._summary = await self._coordinator.async_get_summary(self._meta_id)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._summary is not None


class WearLifetimeHours(_WearBaseSensor):
    SENSOR_KEY = "lifetime_hours"
    _attr_name = "Lifetime hours"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        if self._summary is None:
            return None
        return round(self._summary["lifetime_seconds"] / 3600, 4)


class WearConnectedHours(_WearBaseSensor):
    SENSOR_KEY = "connected_hours"
    _attr_name = "Connected hours"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        if self._summary is None:
            return None
        return round(self._summary["connected_seconds"] / 3600, 4)


class WearLifetimeCycles(_WearBaseSensor):
    SENSOR_KEY = "lifetime_cycles"
    _attr_name = "Lifetime cycles"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "cycles"

    @property
    def native_value(self) -> int | None:
        if self._summary is None:
            return None
        return int(self._summary["lifetime_cycles"])


class WearConnectionDrops(_WearBaseSensor):
    SENSOR_KEY = "connection_drops"
    _attr_name = "Connection drops"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "drops"

    @property
    def native_value(self) -> int | None:
        if self._summary is None:
            return None
        return int(self._summary["connection_drops"])


class WearDutyCycle(_WearBaseSensor):
    SENSOR_KEY = "duty_cycle_pct"
    _attr_name = "Duty cycle"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        if self._summary is None:
            return None
        connected = self._summary["connected_seconds"]
        if connected <= 0:
            return None
        return round(self._summary["lifetime_seconds"] / connected * 100, 1)


class WearPercent(_WearBaseSensor):
    SENSOR_KEY = "wear_pct"
    _attr_name = "Wear"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        if self._summary is None or not self._tracked.rated_hours:
            return None
        hours = self._summary["lifetime_seconds"] / 3600
        return round(hours / self._tracked.rated_hours * 100, 1)


class _RateSensor(_WearBaseSensor):
    """Reads from the coordinator's rolling-rate cache rather than the summary."""

    RATE_KEY: str = ""
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "1/h"
    _attr_suggested_display_precision = 2

    async def _refresh(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coordinator.get_rates(self._meta_id) is not None

    @property
    def native_value(self) -> float | None:
        rates = self._coordinator.get_rates(self._meta_id)
        return round(rates[self.RATE_KEY], 3) if rates else None


class WearFlapRate1h(_RateSensor):
    SENSOR_KEY = "flap_rate_1h"
    RATE_KEY = "flap_rate_1h"
    _attr_name = "Flap rate 1h"


class WearFlapRate24h(_RateSensor):
    SENSOR_KEY = "flap_rate_24h"
    RATE_KEY = "flap_rate_24h"
    _attr_name = "Flap rate 24h"


class WearUnavailRate1h(_RateSensor):
    SENSOR_KEY = "unavail_rate_1h"
    RATE_KEY = "unavail_rate_1h"
    _attr_name = "Unavailable rate 1h"


_SENSOR_CLASSES = (
    WearLifetimeHours,
    WearConnectedHours,
    WearLifetimeCycles,
    WearConnectionDrops,
    WearDutyCycle,
    WearFlapRate1h,
    WearFlapRate24h,
    WearUnavailRate1h,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WearCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[_WearBaseSensor] = []
    for eid in coordinator.tracked_entity_ids():
        tracked = coordinator.get_tracked(eid)
        if tracked is None:
            continue
        via_device = coordinator.get_source_device(eid)
        entities.extend(cls(coordinator, tracked, via_device) for cls in _SENSOR_CLASSES)
        if tracked.rated_hours:
            entities.append(WearPercent(coordinator, tracked, via_device))
    async_add_entities(entities)
