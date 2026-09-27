"""Tests for hub_client.py."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from homeassistant.core import HomeAssistant

from rakopy.model import LevelChangedEvent, SceneChangedEvent

import pytest
from rakopy.errors import SendCommandError
from rakopy.model import Level

from custom_components.rako.hub_client import (
    COMMAND_TIMEOUT,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
    HubClient,
    resync_levels,
    run_event_listener,
    subscribe_to_events,
)
from tests.conftest import MOCK_HOST, MOCK_HUB_ID, MOCK_NAME, make_channel_level


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hub_client(hass: HomeAssistant) -> HubClient:
    """Create a HubClient with the rakopy Hub.__init__ patched out."""
    with patch("custom_components.rako.hub_client.Hub.__init__", return_value=None):
        client = HubClient(
            name=MOCK_NAME,
            host=MOCK_HOST,
            entry_id="test_entry_id",
            hass=hass,
        )
    return client


def _mock_entity(unique_id: str) -> MagicMock:
    """Return a mock entity with a unique_id."""
    entity = MagicMock()
    entity.unique_id = unique_id
    return entity


# ---------------------------------------------------------------------------
# HubClient.hub_id
# ---------------------------------------------------------------------------

async def test_hub_id_property(hass: HomeAssistant) -> None:
    """Test that hub_id reads from the config entry runtime data."""
    client = _make_hub_client(hass)

    mock_entry = MagicMock()
    mock_entry.runtime_data = {"hub_id": MOCK_HUB_ID}
    hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

    assert client.hub_id == MOCK_HUB_ID
    hass.config_entries.async_get_entry.assert_called_once_with("test_entry_id")


# ---------------------------------------------------------------------------
# add / remove entities & event listener lifecycle
# ---------------------------------------------------------------------------

async def test_add_light_starts_event_listener(hass: HomeAssistant) -> None:
    """Adding the first entity should start the event listener task."""
    client = _make_hub_client(hass)

    with patch.object(client, "_try_start_event_listener_task") as mock_start:
        await client.add_light(_mock_entity("light_1"))

    assert "light_1" in client._light_map
    mock_start.assert_called_once()


async def test_add_cover_starts_event_listener(hass: HomeAssistant) -> None:
    """Adding the first cover should start the event listener."""
    client = _make_hub_client(hass)

    with patch.object(client, "_try_start_event_listener_task") as mock_start:
        await client.add_cover(_mock_entity("cover_1"))

    assert "cover_1" in client._cover_map
    mock_start.assert_called_once()


async def test_add_scene_starts_event_listener(hass: HomeAssistant) -> None:
    """Adding the first scene should start the event listener."""
    client = _make_hub_client(hass)

    with patch.object(client, "_try_start_event_listener_task") as mock_start:
        await client.add_scene(_mock_entity("scene_1"))

    assert "scene_1" in client._scene_map
    mock_start.assert_called_once()


async def test_remove_light_cancels_listener_when_last(hass: HomeAssistant) -> None:
    """Removing the last entity should cancel the event listener task."""
    client = _make_hub_client(hass)
    entity = _mock_entity("light_1")
    client._light_map["light_1"] = entity

    with patch.object(client, "_try_cancel_event_listener_task", new_callable=AsyncMock) as mock_cancel:
        await client.remove_light(entity)

    assert "light_1" not in client._light_map
    mock_cancel.assert_awaited_once()


async def test_remove_cover_cancels_listener_when_last(hass: HomeAssistant) -> None:
    """Removing the last cover should cancel the event listener task."""
    client = _make_hub_client(hass)
    entity = _mock_entity("cover_1")
    client._cover_map["cover_1"] = entity

    with patch.object(client, "_try_cancel_event_listener_task", new_callable=AsyncMock) as mock_cancel:
        await client.remove_cover(entity)

    assert "cover_1" not in client._cover_map
    mock_cancel.assert_awaited_once()


async def test_remove_scene_cancels_listener_when_last(hass: HomeAssistant) -> None:
    """Removing the last scene should cancel the event listener task."""
    client = _make_hub_client(hass)
    entity = _mock_entity("scene_1")
    client._scene_map["scene_1"] = entity

    with patch.object(client, "_try_cancel_event_listener_task", new_callable=AsyncMock) as mock_cancel:
        await client.remove_scene(entity)

    assert "scene_1" not in client._scene_map
    mock_cancel.assert_awaited_once()


async def test_remove_unknown_entity_is_noop(hass: HomeAssistant) -> None:
    """Removing an entity that was never added should be a no-op."""
    client = _make_hub_client(hass)
    entity = _mock_entity("nonexistent")

    # Should not raise
    await client.remove_light(entity)
    await client.remove_cover(entity)
    await client.remove_scene(entity)


async def test_try_start_only_on_first_entity(hass: HomeAssistant) -> None:
    """Event listener task is only created when the first entity is added."""
    client = _make_hub_client(hass)

    # Patch hub_id so asyncio.create_task name works
    mock_entry = MagicMock()
    mock_entry.runtime_data = {"hub_id": MOCK_HUB_ID}
    hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

    # Patch the listener to avoid a real (long-running) coroutine
    with patch("custom_components.rako.hub_client.run_event_listener", new_callable=AsyncMock):
        await client.add_light(_mock_entity("l1"))
        task = client._event_listener_task
        assert task is not None

        await client.add_light(_mock_entity("l2"))
        # Task should not have been replaced
        assert client._event_listener_task is task
        await task


async def test_try_cancel_only_when_empty(hass: HomeAssistant) -> None:
    """Event listener task is only cancelled when all entities are removed."""
    client = _make_hub_client(hass)

    # Create an actual asyncio task that we can cancel
    async def noop():
        await asyncio.sleep(3600)

    task = asyncio.create_task(noop())
    client._event_listener_task = task
    client._light_map = {"l1": _mock_entity("l1"), "l2": _mock_entity("l2")}

    await client.remove_light(_mock_entity("l1"))
    # Still one entity left — task should still be running
    assert not task.cancelled()

    await client.remove_light(_mock_entity("l2"))
    # All entities removed — task should be cancelled
    assert task.cancelled()


async def test_add_switch_starts_event_listener(hass: HomeAssistant) -> None:
    """Adding the first switch should start the event listener task."""
    client = _make_hub_client(hass)

    with patch.object(client, "_try_start_event_listener_task") as mock_start:
        await client.add_switch(_mock_entity("s1"))

    assert "s1" in client._switch_map
    mock_start.assert_called_once()


async def test_remove_switch_cancels_listener_when_last(hass: HomeAssistant) -> None:
    """Removing the last switch should cancel the event listener task."""
    client = _make_hub_client(hass)
    client._switch_map = {"s1": _mock_entity("s1")}

    with patch.object(client, "_try_cancel_event_listener_task", new_callable=AsyncMock) as mock_cancel:
        await client.remove_switch(_mock_entity("s1"))

    assert "s1" not in client._switch_map
    mock_cancel.assert_awaited_once()


async def test_switches_count_towards_listener_lifecycle(hass: HomeAssistant) -> None:
    """A registered switch must keep the listener alive after the last light goes."""
    client = _make_hub_client(hass)
    client._light_map = {"l1": _mock_entity("l1")}
    client._switch_map = {"s1": _mock_entity("s1")}

    async def noop():
        await asyncio.sleep(100)

    task = asyncio.create_task(noop())
    client._event_listener_task = task

    await client.remove_light(_mock_entity("l1"))
    assert not task.cancelled()

    await client.remove_switch(_mock_entity("s1"))
    assert task.cancelled()


# ---------------------------------------------------------------------------
# subscribe_to_events
# ---------------------------------------------------------------------------

async def _run_subscribe_with_events(hub_client, events):
    """Helper to run subscribe_to_events with a list of mocked events."""

    async def mock_get_events():
        for event in events:
            yield event

    hub_client.get_events = mock_get_events

    await subscribe_to_events(hub_client)


async def test_subscribe_level_changed_updates_light(hass: HomeAssistant) -> None:
    """LevelChangedEvent should update the matching light's brightness."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID

    light = MagicMock()
    uid = f"{MOCK_HUB_ID}_1_1"
    client._light_map = {uid: light}
    client._cover_map = {}
    client._scene_map = {}

    event = LevelChangedEvent(
        room_id=1, channel_id=1, current_level=100, target_level=200, time_to_take=0, temporary=False
    )
    await _run_subscribe_with_events(client, [event])

    assert light.brightness == 200


async def test_subscribe_level_changed_uses_current_when_no_target(hass: HomeAssistant) -> None:
    """LevelChangedEvent with target_level=None should use current_level."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID

    light = MagicMock()
    uid = f"{MOCK_HUB_ID}_1_1"
    client._light_map = {uid: light}
    client._cover_map = {}
    client._scene_map = {}

    event = LevelChangedEvent(
        room_id=1, channel_id=1, current_level=150, target_level=None, time_to_take=0, temporary=False
    )
    await _run_subscribe_with_events(client, [event])

    assert light.brightness == 150


async def test_subscribe_level_changed_updates_cover(hass: HomeAssistant) -> None:
    """LevelChangedEvent should update the matching cover's position."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID

    cover = MagicMock()
    uid = f"{MOCK_HUB_ID}_2_1"
    client._cover_map = {uid: cover}
    client._light_map = {}
    client._scene_map = {}

    event = LevelChangedEvent(
        room_id=2, channel_id=1, current_level=0, target_level=255, time_to_take=0, temporary=False
    )
    await _run_subscribe_with_events(client, [event])

    assert cover.current_cover_position == 255


async def test_subscribe_scene_changed_updates_scene(hass: HomeAssistant) -> None:
    """SceneChangedEvent should update the matching scene select entity."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID

    scene = MagicMock()
    uid = f"{MOCK_HUB_ID}_1"
    client._scene_map = {uid: scene}
    client._light_map = {}
    client._cover_map = {}

    event = SceneChangedEvent(
        room_id=1, channel_id=0, scene_id=2, active_scene_id=2
    )
    await _run_subscribe_with_events(client, [event])

    assert scene.current_option == 2


async def test_subscribe_handles_exception_gracefully(hass: HomeAssistant) -> None:
    """An exception inside the event loop should be caught and logged."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID

    # Light map accessor raises
    client._light_map = MagicMock()
    client._light_map.__contains__ = MagicMock(side_effect=RuntimeError("boom"))
    client._cover_map = {}
    client._scene_map = {}

    event = LevelChangedEvent(
        room_id=1, channel_id=1, current_level=100, target_level=200, time_to_take=0, temporary=False
    )

    # Should not raise
    await _run_subscribe_with_events(client, [event])


async def test_subscribe_ignores_none_events(hass: HomeAssistant) -> None:
    """None events should be silently skipped."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID
    client._light_map = {}
    client._cover_map = {}
    client._scene_map = {}

    # Should not raise
    await _run_subscribe_with_events(client, [None])


async def test_subscribe_ignores_unmatched_events(hass: HomeAssistant) -> None:
    """Events for entities not in any map should be silently skipped."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID
    client._light_map = {}
    client._cover_map = {}
    client._scene_map = {}

    event = LevelChangedEvent(
        room_id=99, channel_id=99, current_level=100, target_level=200, time_to_take=0, temporary=False
    )
    # Should not raise
    await _run_subscribe_with_events(client, [event])


async def test_subscribe_level_changed_updates_switch(hass: HomeAssistant) -> None:
    """LevelChangedEvent should update the matching switch's level."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID

    switch = MagicMock()
    uid = f"{MOCK_HUB_ID}_11_1"
    client._switch_map = {uid: switch}
    client._light_map = {}
    client._cover_map = {}
    client._scene_map = {}

    event = LevelChangedEvent(
        room_id=11, channel_id=1, current_level=0, target_level=255, time_to_take=0, temporary=False
    )
    await _run_subscribe_with_events(client, [event])

    assert switch.level == 255


# ---------------------------------------------------------------------------
# run_event_listener - reconnect after the hub drops the feed (27/09/2026)
# ---------------------------------------------------------------------------

async def _run_listener(subscribe_effects, resync_effects=None):
    """Run run_event_listener with the feed and resync patched.

    The last subscribe effect should be CancelledError, which ends the loop the
    way cancelling the task does. Returns (sleep mock, resync mock).
    """
    client = MagicMock()
    sleep = AsyncMock()
    resync = AsyncMock(side_effect=resync_effects)
    with (
        patch("custom_components.rako.hub_client.subscribe_to_events",
              AsyncMock(side_effect=subscribe_effects)),
        patch("custom_components.rako.hub_client.resync_levels", resync),
        patch("custom_components.rako.hub_client.asyncio.sleep", sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_event_listener(client)
    return sleep, resync


async def test_listener_reconnects_after_feed_error_and_after_feed_end() -> None:
    """A dropped feed (error or clean end) is reopened, with a resync first."""
    sleep, resync = await _run_listener(
        [ConnectionResetError("hub rebooted"), None, asyncio.CancelledError()]
    )

    # No resync before the first connection; one before each reconnect.
    assert resync.await_count == 2
    # A successful resync resets the wait, so both waits are the minimum.
    assert [c.args[0] for c in sleep.await_args_list] == [RECONNECT_DELAY_MIN] * 2


async def test_listener_backs_off_while_hub_unreachable() -> None:
    """While the hub stays away the wait doubles, capped at the maximum."""
    sleep, resync = await _run_listener(
        [ConnectionResetError(), asyncio.CancelledError()],
        resync_effects=[OSError("no route")] * 5 + [None],
    )

    assert [c.args[0] for c in sleep.await_args_list] == [
        RECONNECT_DELAY_MIN, 10, 20, 40, RECONNECT_DELAY_MAX, RECONNECT_DELAY_MAX,
    ]
    assert resync.await_count == 6


async def test_listener_cancellation_is_not_swallowed() -> None:
    """Cancelling the task (last entity removed, unload) stops the loop."""
    sleep, resync = await _run_listener([asyncio.CancelledError()])
    sleep.assert_not_awaited()
    resync.assert_not_awaited()


# ---------------------------------------------------------------------------
# resync_levels / drop_command_connection
# ---------------------------------------------------------------------------

async def test_resync_updates_every_entity_type() -> None:
    """A resync brings lights, covers, switches and scenes up to date."""
    client = MagicMock()
    client.hub_id = MOCK_HUB_ID
    light, cover, switch, scene = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    client._light_map = {f"{MOCK_HUB_ID}_1_1": light}
    client._cover_map = {f"{MOCK_HUB_ID}_2_1": cover}
    client._switch_map = {f"{MOCK_HUB_ID}_11_1": switch}
    client._scene_map = {f"{MOCK_HUB_ID}_1": scene}
    client.get_levels = AsyncMock(return_value=[
        Level(room_id=1, current_scene_id=3, channel_levels=[make_channel_level(1, 40, 200)]),
        Level(room_id=2, current_scene_id=0, channel_levels=[make_channel_level(1, 255)]),
        Level(room_id=11, current_scene_id=0, channel_levels=[make_channel_level(1, 0)]),
    ])

    await resync_levels(client)

    assert light.brightness == 200  # target level wins while fading
    assert cover.current_cover_position == 255
    assert switch.level == 0
    assert scene.current_option == 3


async def test_resync_failure_drops_the_command_connection() -> None:
    """A failed read forgets the half-open socket so the next try reconnects."""
    client = MagicMock()
    client.get_levels = AsyncMock(side_effect=ValueError("empty reply"))

    with pytest.raises(ValueError):
        await resync_levels(client)

    client.drop_command_connection.assert_called_once()


async def test_drop_command_connection_closes_and_clears(hass: HomeAssistant) -> None:
    """The writer is closed and both streams forgotten."""
    client = _make_hub_client(hass)
    writer = MagicMock()
    client._writer = writer
    client._reader = MagicMock()

    client.drop_command_connection()

    writer.close.assert_called_once()
    assert client._writer is None
    assert client._reader is None


async def test_drop_command_connection_without_a_connection(hass: HomeAssistant) -> None:
    """Dropping before anything was opened is harmless."""
    client = _make_hub_client(hass)
    client.drop_command_connection()
    assert client._writer is None


# ---------------------------------------------------------------------------
# Commands and queries: time limit, one retry on a fresh connection
# ---------------------------------------------------------------------------

async def test_with_retry_returns_first_success(hass: HomeAssistant) -> None:
    """A working hub is asked once."""
    client = _make_hub_client(hass)
    call = AsyncMock(return_value="ok")
    with patch.object(client, "drop_command_connection") as drop:
        assert await client._with_retry(call, 1) == "ok"
    call.assert_awaited_once_with(1)
    drop.assert_not_called()


async def test_with_retry_reconnects_once_after_dead_connection(hass: HomeAssistant) -> None:
    """A dead socket is dropped and the request repeated on a new one."""
    client = _make_hub_client(hass)
    call = AsyncMock(side_effect=[ValueError("empty reply"), "ok"])
    with patch.object(client, "drop_command_connection") as drop:
        assert await client._with_retry(call) == "ok"
    assert call.await_count == 2
    drop.assert_called_once()


async def test_with_retry_gives_up_after_second_failure(hass: HomeAssistant) -> None:
    """Two connection failures in a row are raised to the caller."""
    client = _make_hub_client(hass)
    call = AsyncMock(side_effect=ConnectionResetError("hub gone"))
    with patch.object(client, "drop_command_connection") as drop, \
            pytest.raises(ConnectionResetError):
        await client._with_retry(call)
    assert call.await_count == 2
    assert drop.call_count == 2


async def test_with_retry_does_not_repeat_a_refused_command(hass: HomeAssistant) -> None:
    """The hub answering "no" is not a connection fault: no retry."""
    client = _make_hub_client(hass)
    call = AsyncMock(side_effect=SendCommandError("bad room"))
    with patch.object(client, "drop_command_connection") as drop, \
            pytest.raises(SendCommandError):
        await client._with_retry(call)
    call.assert_awaited_once()
    drop.assert_not_called()


async def test_with_retry_times_out_a_hub_that_never_replies(hass: HomeAssistant) -> None:
    """A reply that never comes no longer blocks every later command."""
    client = _make_hub_client(hass)

    async def never_replies():
        await asyncio.sleep(3600)

    with patch("custom_components.rako.hub_client.COMMAND_TIMEOUT", 0.05), \
            patch.object(client, "drop_command_connection") as drop, \
            pytest.raises(TimeoutError):
        await client._with_retry(never_replies)
    assert drop.call_count == 2


async def test_send_and_query_go_through_the_retry(hass: HomeAssistant) -> None:
    """rakopy's _send and _query are both wrapped."""
    client = _make_hub_client(hass)
    with patch.object(client, "_with_retry", AsyncMock(return_value=["x"])) as wr:
        await client._send({"name": "send"})
        assert await client._query("LEVEL", str, 3) == ["x"]
    assert wr.await_count == 2
    assert wr.await_args_list[1].args[1:] == ("LEVEL", str, 3)


def test_command_timeout_is_sane() -> None:
    """Long enough for a busy hub, short enough that a stuck one is noticed."""
    assert 3 <= COMMAND_TIMEOUT <= 30
