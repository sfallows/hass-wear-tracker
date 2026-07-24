"""Health-alert binary sensor (DESIGN §6) — on when an entity's flap or
connection rate is anomalously high vs its 30-day baseline."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_WEAR_UPDATED
from .coordinator import WearCoordinator
from .registry import TrackedEntity, wear_sensor_root


class HealthAlertBinarySensor(BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Health alert"

    def __init__(
        self,
        coordinator: WearCoordinator,
        tracked: TrackedEntity,
        via_device: tuple[str, str] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._meta_id = tracked.id
        self._attr_unique_id = f"wear_tracker_{wear_sensor_root(tracked)}_health_alert"
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
            async_dispatcher_connect(self.hass, SIGNAL_WEAR_UPDATED, self._on_signal)
        )
        self.async_write_ha_state()

    @callback
    def _on_signal(self, meta_id: int) -> None:
        if meta_id == self._meta_id:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coordinator.get_rates(self._meta_id) is not None

    @property
    def is_on(self) -> bool:
        return self._coordinator.get_health(self._meta_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WearCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for eid in coordinator.tracked_entity_ids():
        tracked = coordinator.get_tracked(eid)
        if tracked is None:
            continue
        entities.append(
            HealthAlertBinarySensor(coordinator, tracked, coordinator.get_source_device(eid))
        )
    async_add_entities(entities)
