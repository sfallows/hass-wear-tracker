"""Repair issues for newly-appeared trackable devices (DESIGN §8).

When discovery_mode is `prompt`, the coordinator raises a fixable issue per new
device. The fix flow offers Track / Skip / Never. Track and Never edit the config
entry options (which reloads the entry); completing the flow clears the issue.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import CONF_ENTITIES, CONF_EXCLUDED_DOMAINS, DOMAIN


def issue_id(entity_id: str) -> str:
    return f"new_device_{entity_id}"


@callback
def async_create_new_device_issue(
    hass: HomeAssistant, entity_id: str, domain: str
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entity_id),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="new_device",
        translation_placeholders={"entity_id": entity_id, "domain": domain},
        data={"entity_id": entity_id, "domain": domain},
    )


def _entry(hass: HomeAssistant) -> ConfigEntry | None:
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


class NewDeviceRepairFlow(RepairsFlow):
    def __init__(self, data: dict[str, Any]) -> None:
        self._entity_id = data["entity_id"]
        self._domain = data["domain"]

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init", menu_options=["track", "skip", "never"]
        )

    async def async_step_track(self, user_input=None):
        entry = _entry(self.hass)
        if entry is not None:
            entities = list(entry.options.get(CONF_ENTITIES, []))
            if self._entity_id not in entities:
                entities.append(self._entity_id)
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_ENTITIES: entities}
                )
        return self.async_create_entry(title="", data={})

    async def async_step_skip(self, user_input=None):
        return self.async_create_entry(title="", data={})

    async def async_step_never(self, user_input=None):
        entry = _entry(self.hass)
        if entry is not None:
            excluded = list(entry.options.get(CONF_EXCLUDED_DOMAINS, []))
            if self._domain not in excluded:
                excluded.append(self._domain)
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_EXCLUDED_DOMAINS: excluded}
                )
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(hass, issue_id, data):  # noqa: ARG001 - HA signature
    return NewDeviceRepairFlow(data or {})
