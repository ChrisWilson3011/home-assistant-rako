"""Tests for switch.py."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.rako.hub_client import subscribe_to_events
from custom_components.rako.switch import RakoSwitchEntity, async_setup_entry
from rakopy.errors import SendCommandError
from rakopy.model import Channel, Level, LevelChangedEvent, Room, SceneChangedEvent

from tests.conftest import MOCK_HUB_ID, make_channel_level


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_switch(
    hub_client,
    room: Room | None = None,
    channel: Channel | None = None,
    current_level: int = 0,
    target_level: int | None = None,
) -> RakoSwitchEntity:
    """Convenience factory for RakoSwitchEntity."""
    if channel is None:
        channel = Channel(id=1, title="Extract Fan", type="SWITCH", color_type=None, color_title=None, multi_channel_component=None)
    if room is None:
        room = Room(id=11, title="Extract Fan", type="SWITCH", mode=None, channels=[channel], scenes=[])
    cl = make_channel_level(channel.id, current_level, target_level)
    return RakoSwitchEntity(hub_client=hub_client, room=room, channel=channel, channel_level=cl)


def _levels_for_room(room_id: int, channel_levels: dict[int, int]) -> Level:
    """Build a Level for a room from a {channel_id: level} mapping."""
    return Level(
        room_id=room_id,
        current_scene_id=0,
        channel_levels=[make_channel_level(cid, lvl) for cid, lvl in channel_levels.items()],
    )


def _level_event(room_id: int, channel_id: int, current: int, target: int | None) -> LevelChangedEvent:
    return LevelChangedEvent(
        room_id=room_id, channel_id=channel_id, current_level=current,
        target_level=target, time_to_take=1591, temporary=False,
    )


async def _run_events(hub_client, events) -> None:
    async def mock_get_events():
        for event in events:
            yield event

    hub_client.get_events = mock_get_events
    await subscribe_to_events(hub_client)


def _event_client(switch: RakoSwitchEntity) -> MagicMock:
    """A hub client whose only registered entity is the given switch."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID
    client._light_map = {}
    client._cover_map = {}
    client._scene_map = {}
    client._switch_map = {switch.unique_id: switch}
    return client


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------

async def test_setup_entry_discovers_switch_rooms(
    hass: HomeAssistant,
    mock_hub_client,
    mock_config_entry,
    mock_switch_room,
) -> None:
    """SWITCH rooms should produce one switch entity per configured channel."""
    mock_hub_client.get_rooms.return_value = [mock_switch_room]
    mock_hub_client.get_levels.return_value = [_levels_for_room(11, {0: 0, 1: 255})]
    added: list = []

    await async_setup_entry(hass, mock_config_entry, lambda entities, _: added.extend(entities))

    assert len(added) == 1
    assert isinstance(added[0], RakoSwitchEntity)
    assert added[0].name == "Extract Fan"
    assert added[0].is_on is True


async def test_setup_entry_no_room_level_entity(
    hass: HomeAssistant,
    mock_hub_client,
    mock_config_entry,
    mock_switch_room,
) -> None:
    """Channel 0 and unconfigured channel slots must not become entities.

    The hub reports 16 channel slots for every room; a switch room with one
    configured channel should produce exactly one entity.
    """
    mock_hub_client.get_rooms.return_value = [mock_switch_room]
    mock_hub_client.get_levels.return_value = [
        _levels_for_room(11, {cid: 255 for cid in range(16)})
    ]
    added: list = []

    await async_setup_entry(hass, mock_config_entry, lambda entities, _: added.extend(entities))

    assert [e.unique_id for e in added] == [f"{MOCK_HUB_ID}_11_1"]


async def test_setup_entry_ignores_other_room_types(
    hass: HomeAssistant,
    mock_hub_client,
    mock_config_entry,
) -> None:
    """LIGHT and BLIND rooms from the default fixtures should not become switches."""
    added: list = []

    await async_setup_entry(hass, mock_config_entry, lambda entities, _: added.extend(entities))

    assert len(added) == 0


async def test_setup_entry_missing_room_levels(
    hass: HomeAssistant,
    mock_hub_client,
    mock_config_entry,
    mock_switch_room,
) -> None:
    """A switch room with no levels should be skipped, not crash."""
    mock_hub_client.get_rooms.return_value = [mock_switch_room]
    mock_hub_client.get_levels.return_value = []
    added: list = []

    await async_setup_entry(hass, mock_config_entry, lambda entities, _: added.extend(entities))

    assert len(added) == 0


async def test_setup_entry_missing_channel_level(
    hass: HomeAssistant,
    mock_hub_client,
    mock_config_entry,
    mock_switch_room,
) -> None:
    """A switch channel with no level should be skipped, not crash."""
    mock_hub_client.get_rooms.return_value = [mock_switch_room]
    mock_hub_client.get_levels.return_value = [_levels_for_room(11, {0: 0})]
    added: list = []

    await async_setup_entry(hass, mock_config_entry, lambda entities, _: added.extend(entities))

    assert len(added) == 0


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def test_is_on_true(mock_hub_client) -> None:
    switch = _make_switch(mock_hub_client, current_level=255)
    assert switch.is_on is True


def test_is_on_false(mock_hub_client) -> None:
    switch = _make_switch(mock_hub_client, current_level=0)
    assert switch.is_on is False


def test_initial_state_uses_target_level(mock_hub_client) -> None:
    """While fading, the target level is the state the switch is heading to."""
    switch = _make_switch(mock_hub_client, current_level=0, target_level=255)
    assert switch.is_on is True


def test_name_is_channel_title(mock_hub_client) -> None:
    ch = Channel(id=1, title="Towel Rail", type="SWITCH", color_type=None, color_title=None, multi_channel_component=None)
    switch = _make_switch(mock_hub_client, channel=ch)
    assert switch.name == "Towel Rail"


def test_should_poll(mock_hub_client) -> None:
    assert _make_switch(mock_hub_client).should_poll is False


def test_unique_id(mock_hub_client) -> None:
    ch = Channel(id=1, title="Towel Rail", type="SWITCH", color_type=None, color_title=None, multi_channel_component=None)
    room = Room(id=13, title="Towel Rail", type="SWITCH", mode=None, channels=[ch], scenes=[])
    switch = _make_switch(mock_hub_client, room=room, channel=ch)
    assert switch.unique_id == f"{MOCK_HUB_ID}_13_1"


# ---------------------------------------------------------------------------
# Turn on / off
# ---------------------------------------------------------------------------

async def test_turn_on(mock_hub_client) -> None:
    switch = _make_switch(mock_hub_client, current_level=0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(switch, "async_write_ha_state", lambda: None)
        await switch.async_turn_on()

    mock_hub_client.set_level.assert_awaited_once_with(11, 1, 255)
    assert switch.is_on is True


async def test_turn_off(mock_hub_client) -> None:
    switch = _make_switch(mock_hub_client, current_level=255)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(switch, "async_write_ha_state", lambda: None)
        await switch.async_turn_off()

    mock_hub_client.set_level.assert_awaited_once_with(11, 1, 0)
    assert switch.is_on is False


async def test_turn_on_send_error_keeps_state(mock_hub_client) -> None:
    """A failed command should be logged and leave the state unchanged."""
    mock_hub_client.set_level.side_effect = SendCommandError("fail")
    switch = _make_switch(mock_hub_client, current_level=0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(switch, "async_write_ha_state", lambda: None)
        await switch.async_turn_on()

    assert switch.is_on is False


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def test_added_to_hass_registers(mock_hub_client) -> None:
    switch = _make_switch(mock_hub_client)
    await switch.async_added_to_hass()
    mock_hub_client.add_switch.assert_awaited_once_with(switch)


async def test_will_remove_from_hass_deregisters(mock_hub_client) -> None:
    switch = _make_switch(mock_hub_client)
    await switch.async_will_remove_from_hass()
    mock_hub_client.remove_switch.assert_awaited_once_with(switch)


# ---------------------------------------------------------------------------
# Event sequences captured from a real hub (firmware 3.8.2)
# ---------------------------------------------------------------------------

async def test_keypad_press_and_hold_off_stays_off(mock_hub_client) -> None:
    """Press-and-hold off at a keypad must leave the switch off.

    The keypad first sends a room-wide raise (scene -1 on channel 0), which
    the hub reports as every unconfigured channel slot heading to 255, then
    sends the real off to the configured channel only.
    """
    ch = Channel(id=1, title="Towel Rail", type="SWITCH", color_type=None, color_title=None, multi_channel_component=None)
    room = Room(id=13, title="Towel Rail", type="SWITCH", mode=None, channels=[ch], scenes=[])
    switch = _make_switch(mock_hub_client, room=room, channel=ch, current_level=255)
    states: list[bool] = []
    switch.async_write_ha_state = lambda: states.append(switch.is_on)

    events = [SceneChangedEvent(room_id=13, channel_id=0, scene_id=-1, active_scene_id=0)]
    events += [_level_event(13, cid, 0, 255) for cid in [0] + list(range(2, 16))]
    events.append(_level_event(13, 1, 255, 0))

    await _run_events(_event_client(switch), events)

    assert switch.is_on is False
    # The phantom raise on other channels never flipped the switch on
    assert states == [False]


async def test_keypad_on_at_wall(mock_hub_client) -> None:
    """Switching on at the keypad is reported as the channel heading to 255."""
    switch = _make_switch(mock_hub_client, current_level=0)
    switch.async_write_ha_state = lambda: None

    await _run_events(_event_client(switch), [_level_event(11, 1, 0, 255)])

    assert switch.is_on is True


async def test_global_off_switches_off(mock_hub_client) -> None:
    """A global off reaching another room and the switch turns the switch off."""
    switch = _make_switch(mock_hub_client, current_level=255)
    switch.async_write_ha_state = lambda: None

    events = [_level_event(5, 1, 0, 0), _level_event(11, 1, 255, 0)]
    await _run_events(_event_client(switch), events)

    assert switch.is_on is False


async def test_event_without_target_uses_current(mock_hub_client) -> None:
    switch = _make_switch(mock_hub_client, current_level=0)
    switch.async_write_ha_state = lambda: None

    await _run_events(_event_client(switch), [_level_event(11, 1, 255, None)])

    assert switch.is_on is True
