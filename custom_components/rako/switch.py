"""Rako platform for switch integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError
from rakopy.model import Channel, ChannelLevel, Room
from .hub_client import COMMAND_ERRORS, HubClient
from .model import RakoDomainEntryData

_LOGGER = logging.getLogger(__name__)

SWITCH_ON_LEVEL = 255
SWITCH_OFF_LEVEL = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the config entry."""
    rako_domain_entry_data: RakoDomainEntryData = entry.runtime_data
    hub_client = rako_domain_entry_data["hub_client"]

    levels_lookup = {}
    levels = await hub_client.get_levels()
    for level in levels:
        levels_lookup[level.room_id] = {}
        for channel_level in level.channel_levels:
            levels_lookup[level.room_id][channel_level.channel_id] = channel_level

    switches: list[Entity] = []

    rooms = await hub_client.get_rooms()
    for room in rooms:
        if room.type == "SWITCH":
            room_levels = levels_lookup.get(room.id, None)
            if room_levels is not None:
                # One entity per configured channel only. No room-level
                # (channel 0) entity: a keypad press-and-hold sends a
                # room-wide raise that sets every channel slot to 255,
                # including unconfigured ones, before switching the real
                # channel off.
                for channel in room.channels:
                    channel_level = room_levels.get(channel.id, None)
                    if channel_level is not None:
                        switches.append(
                            RakoSwitchEntity(
                                hub_client=hub_client,
                                room=room,
                                channel=channel,
                                channel_level=channel_level
                            )
                        )
                    else:
                        _LOGGER.warning(
                            "Cannot find levels for room %s and channel %s",
                            room.id, channel.id
                        )
            else:
                _LOGGER.warning("Cannot find levels for room %s", room.id)

    async_add_entities(switches, True)


class RakoSwitchEntity(SwitchEntity):
    """Representation of a Rako Switch (e.g. extract fan, towel rail)."""

    def __init__(
        self,
        hub_client: HubClient,
        room: Room,
        channel: Channel,
        channel_level: ChannelLevel
    ) -> None:
        """Initialize a RakoSwitchEntity."""
        self._hub_client = hub_client
        self._room = room
        self._channel = channel
        if channel_level.target_level is not None:
            self._level = channel_level.target_level
        else:
            self._level = channel_level.current_level

    @property
    def level(self) -> int:
        """Return the Rako level (0-255) of the switch channel."""
        return self._level

    @level.setter
    def level(self, value: int) -> None:
        """Set the level. Used when state is updated outside Home Assistant."""
        self._level = value
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._level > 0

    @property
    def name(self) -> str:
        """Return the display name of this switch."""
        return self._channel.title

    @property
    def should_poll(self) -> bool:
        """Entity pushes its state to HA."""
        return False

    @property
    def unique_id(self) -> str:
        """Switch's unique ID."""
        return f"{self._hub_client.hub_id}_{self._room.id}_{self._channel.id}"

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await self._hub_client.add_switch(self)

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity about to be removed from hass."""
        await self._hub_client.remove_switch(self)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._async_set_level(SWITCH_ON_LEVEL)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._async_set_level(SWITCH_OFF_LEVEL)

    async def _async_set_level(self, level: int) -> None:
        """Send a level to the switch channel and update state optimistically."""
        try:
            await self._hub_client.set_level(self._room.id, self._channel.id, level)
            self.level = level
        except COMMAND_ERRORS as err:
            raise HomeAssistantError(
                f"Rako hub did not change {self.name}: {err!r}"
            ) from err
