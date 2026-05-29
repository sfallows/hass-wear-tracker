"""Config + options flow (DESIGN §8).

Single instance: the integration is one global tracker over a single SQLite DB.
The user flow scans the registry and offers a per-domain bulk-confirm, then a
discovery mode for entities that appear later. YAML config is imported into an
entry so v0.1 users migrate without losing data.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import discovery
from .const import (
    CONF_AUTO_TRACK,
    CONF_DISCOVERY_MODE,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    CONF_EXCLUDED_ENTITIES,
    CONF_INCLUDE_BINARY_SENSORS,
    DISCOVERY_AUTO_TRACK,
    DISCOVERY_MODES,
    DISCOVERY_OFF,
    DISCOVERY_PROMPT,
    DOMAIN,
)

_TITLE = "Wear Tracker"
_BINARY_SENSOR = "binary_sensor"

_DISCOVERY_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(DISCOVERY_MODES),
        translation_key="discovery_mode",
        mode=SelectSelectorMode.LIST,
    )
)


class WearTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._by_domain: dict[str, list[str]] = {}
        self._selected: list[str] = []

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Migrate a v0.1 YAML config into a config entry.

        `single_config_entry` in the manifest makes HA abort this if an entry
        already exists, so repeated YAML imports on restart are harmless.
        """
        mode = DISCOVERY_AUTO_TRACK if import_data.get(CONF_AUTO_TRACK) else DISCOVERY_OFF
        return self.async_create_entry(
            title=f"{_TITLE} (YAML)",
            data={},
            options={
                CONF_ENTITIES: list(import_data.get(CONF_ENTITIES, [])),
                CONF_DISCOVERY_MODE: mode,
                CONF_INCLUDE_BINARY_SENSORS: import_data.get(
                    CONF_INCLUDE_BINARY_SENSORS, False
                ),
                CONF_EXCLUDED_ENTITIES: list(import_data.get(CONF_EXCLUDE, [])),
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._by_domain = discovery.trackable_by_domain(self.hass)
        return await self.async_step_bulk_confirm()

    async def async_step_bulk_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            for domain, entity_ids in self._by_domain.items():
                if user_input.get(domain):
                    self._selected.extend(entity_ids)
            return await self.async_step_discovery_mode()

        schema: dict[Any, Any] = {}
        for domain in sorted(self._by_domain):
            default = domain != _BINARY_SENSOR
            schema[vol.Optional(domain, default=default)] = bool
        summary = ", ".join(
            f"{len(ids)} {domain}" for domain, ids in sorted(self._by_domain.items())
        )
        return self.async_show_form(
            step_id="bulk_confirm",
            data_schema=vol.Schema(schema),
            description_placeholders={"summary": summary or "no trackable entities found"},
        )

    async def async_step_discovery_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=_TITLE,
                data={},
                options={
                    CONF_ENTITIES: self._selected,
                    CONF_DISCOVERY_MODE: user_input[CONF_DISCOVERY_MODE],
                    CONF_INCLUDE_BINARY_SENSORS: bool(self._by_domain.get(_BINARY_SENSOR))
                    and any(eid in self._selected for eid in self._by_domain.get(_BINARY_SENSOR, [])),
                },
            )
        schema = vol.Schema(
            {vol.Required(CONF_DISCOVERY_MODE, default=DISCOVERY_PROMPT): _DISCOVERY_SELECTOR}
        )
        return self.async_show_form(step_id="discovery_mode", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WearTrackerOptionsFlow()


class WearTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self.config_entry.options
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={**options, **user_input},
            )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DISCOVERY_MODE,
                    default=options.get(CONF_DISCOVERY_MODE, DISCOVERY_PROMPT),
                ): _DISCOVERY_SELECTOR,
                vol.Required(
                    CONF_INCLUDE_BINARY_SENSORS,
                    default=options.get(CONF_INCLUDE_BINARY_SENSORS, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
