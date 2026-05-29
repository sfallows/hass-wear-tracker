"""Config flow: user wizard, single instance, YAML import."""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

DOMAIN = "wear_tracker"


async def test_user_flow_walks_wizard_and_creates_entry(hass, enable_custom_integrations):
    er.async_get(hass).async_get_or_create(
        "light", "demo", "uid-1", suggested_object_id="kitchen"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "bulk_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"light": True}
    )
    assert result["step_id"] == "discovery_mode"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"discovery_mode": "off"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"]["entities"] == ["light.kitchen"]
    assert result["options"]["discovery_mode"] == "off"
    # The new entry is set up automatically; let it finish before teardown.
    await hass.async_block_till_done()


async def test_single_instance_aborts_second_setup(hass, enable_custom_integrations):
    MockConfigEntry(domain=DOMAIN, data={}, options={"entities": []}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_yaml_import_creates_entry(hass, enable_custom_integrations):
    assert await async_setup_component(
        hass,
        DOMAIN,
        {DOMAIN: {"entities": ["light.kitchen_island"], "auto_track": True}},
    )
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].options["entities"] == ["light.kitchen_island"]
    assert entries[0].options["discovery_mode"] == "auto_track"
