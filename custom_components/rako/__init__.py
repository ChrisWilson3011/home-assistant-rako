"""The Rako integration."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from .const import DOMAIN
from .hub_client import HubClient
from .model import RakoDomainEntryData

# How long to wait for the hub to answer at start-up before trying again later.
SETUP_TIMEOUT = 10

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SELECT, Platform.COVER, Platform.SWITCH]

type RakoConfigEntry = ConfigEntry[RakoDomainEntryData]


async def async_setup_entry(hass: HomeAssistant, entry: RakoConfigEntry) -> bool:
    """Set up Rako from a config entry."""
    hub_client = HubClient(
        name=entry.data[CONF_NAME],
        host=entry.data[CONF_HOST],
        entry_id=entry.entry_id,
        hass=hass
    )

    # A hub that does not answer at start-up (rebooting, a network blip, Home
    # Assistant up before the LAN) used to fail setup for good: "Failed to set
    # up" until someone pressed Reload. ConfigEntryNotReady makes Home
    # Assistant retry on its own, backing off between attempts.
    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            hub_info = await hub_client.get_hub_status()
    except (OSError, TimeoutError, ValueError, KeyError) as err:
        hub_client.drop_command_connection()
        raise ConfigEntryNotReady(
            f"Rako hub at {entry.data[CONF_HOST]} did not answer: {err!r}"
        ) from err

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, hub_info.mac_address)},
        identifiers={(DOMAIN, hub_info.id)},
        manufacturer="Rako",
        name="Hub"
    )

    rako_domain_entry_data: RakoDomainEntryData = {
        "hub_id": hub_info.id,
        "hub_client": hub_client
    }

    entry.runtime_data = rako_domain_entry_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: RakoConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
