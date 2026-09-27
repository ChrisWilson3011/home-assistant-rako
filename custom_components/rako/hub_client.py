"""Rako integration client for Hub."""
from asyncio import Task
import asyncio
import contextlib
import logging

from homeassistant.components.cover import CoverEntity
from homeassistant.components.light import LightEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from rakopy.hub import Hub
from rakopy.model import LevelChangedEvent, SceneChangedEvent
from .model import RakoDomainEntryData

_LOGGER = logging.getLogger(__name__)

# Wait before reconnecting the event feed after it drops, doubling up to the
# maximum while the hub stays unreachable.
RECONNECT_DELAY_MIN = 5
RECONNECT_DELAY_MAX = 60


class HubClient(Hub):
    """Rako Hub Client."""

    def __init__(
        self,
        name: str,
        host: str,
        entry_id: str,
        hass: HomeAssistant,
    ) -> None:
        """Init subclass of rakopy hub."""
        super().__init__(name, host)
        self.entry_id = entry_id
        self.hass = hass

        self._event_listener_task: Task | None = None
        self._cover_map: dict[str, CoverEntity] = {}
        self._light_map: dict[str, LightEntity] = {}
        self._scene_map: dict[str, SelectEntity] = {}
        self._switch_map: dict[str, SwitchEntity] = {}

    @property
    def hub_id(self) -> str:
        """Return Hub Id."""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        rako_domain_entry_data: RakoDomainEntryData = entry.runtime_data

        return rako_domain_entry_data['hub_id']

    async def add_cover(self, cover: CoverEntity) -> None:
        """Register a cover to listen for state updates."""
        self._cover_map[cover.unique_id] = cover
        self._try_start_event_listener_task()

    async def add_light(self, light: LightEntity) -> None:
        """Register a light to listen for state updates."""
        self._light_map[light.unique_id] = light
        self._try_start_event_listener_task()

    async def add_scene(self, select: SelectEntity) -> None:
        """Register a select to listen for state updates."""
        self._scene_map[select.unique_id] = select
        self._try_start_event_listener_task()

    async def add_switch(self, switch: SwitchEntity) -> None:
        """Register a switch to listen for state updates."""
        self._switch_map[switch.unique_id] = switch
        self._try_start_event_listener_task()

    async def remove_cover(self, cover: CoverEntity) -> None:
        """Deregister a cover to listen for state updates."""
        if cover.unique_id in self._cover_map:
            del self._cover_map[cover.unique_id]
            await self._try_cancel_event_listener_task()

    async def remove_light(self, light: LightEntity) -> None:
        """Deregister a light to listen for state updates."""
        if light.unique_id in self._light_map:
            del self._light_map[light.unique_id]
            await self._try_cancel_event_listener_task()

    async def remove_scene(self, select: SelectEntity) -> None:
        """Deregister a select to listen for state updates."""
        if select.unique_id in self._scene_map:
            del self._scene_map[select.unique_id]
            await self._try_cancel_event_listener_task()

    async def remove_switch(self, switch: SwitchEntity) -> None:
        """Deregister a switch to listen for state updates."""
        if switch.unique_id in self._switch_map:
            del self._switch_map[switch.unique_id]
            await self._try_cancel_event_listener_task()

    def _total_entities(self) -> int:
        """Return the number of entities registered for state updates."""
        return (
            len(self._light_map) + len(self._scene_map)
            + len(self._cover_map) + len(self._switch_map)
        )

    def _try_start_event_listener_task(self) -> None:
        """Start the event listener task."""
        total_entities = self._total_entities()
        if total_entities == 1:
            self._event_listener_task: Task = asyncio.create_task(
                run_event_listener(self), name=f"rako_{self.hub_id}_event_listener_task"
            )

    def drop_command_connection(self) -> None:
        """Forget the command connection so the next request opens a new one.

        rakopy only reconnects when the socket is already marked closing. After
        the hub reboots, the old socket can sit half-open: every read returns
        nothing and every command fails until Home Assistant is restarted.
        """
        writer = getattr(self, "_writer", None)
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        self._reader = None
        self._writer = None

    async def _try_cancel_event_listener_task(self) -> None:
        """Try to cancel event listener task."""
        total_entities = self._total_entities()
        if total_entities == 0:
            if event_listener_task := self._event_listener_task:
                event_listener_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await event_listener_task


async def run_event_listener(hub_client: HubClient) -> None:
    """Keep the hub's event feed running for as long as entities need it.

    The feed ends or raises when the hub drops the connection (a reboot, a
    network blip). Without this loop the listener task died with it and Home
    Assistant stopped hearing about changes until the integration was
    reloaded. Each reconnect first re-reads every level, so anything that
    changed while the feed was down is picked up.
    """
    delay = RECONNECT_DELAY_MIN
    first_run = True
    while True:
        try:
            if not first_run:
                await resync_levels(hub_client)
                delay = RECONNECT_DELAY_MIN
            first_run = False
            await subscribe_to_events(hub_client)
            _LOGGER.warning("Rako hub closed the event feed; reconnecting in %s s", delay)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any failure means reconnect
            first_run = False
            _LOGGER.warning(
                "Rako hub event feed lost (%s); reconnecting in %s s", repr(err), delay
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_DELAY_MAX)


async def resync_levels(hub_client: HubClient) -> None:
    """Re-read every level from the hub and update the matching entities."""
    try:
        levels = await hub_client.get_levels()
    except Exception:
        hub_client.drop_command_connection()
        raise

    for level in levels:
        scene_id = f"{hub_client.hub_id}_{level.room_id}"
        if scene_id in hub_client._scene_map:
            hub_client._scene_map[scene_id].current_option = level.current_scene_id

        for channel_level in level.channel_levels:
            unique_id = f"{hub_client.hub_id}_{level.room_id}_{channel_level.channel_id}"
            value = (
                channel_level.target_level
                if channel_level.target_level is not None
                else channel_level.current_level
            )
            if unique_id in hub_client._light_map:
                hub_client._light_map[unique_id].brightness = value
            if unique_id in hub_client._cover_map:
                hub_client._cover_map[unique_id].current_cover_position = value
            if unique_id in hub_client._switch_map:
                hub_client._switch_map[unique_id].level = value


async def subscribe_to_events(hub_client: HubClient) -> None:
    """Handle events from the hub until the feed ends or fails."""
    async for event in hub_client.get_events():
        try:
            if event and isinstance(event, LevelChangedEvent):
                unique_id = f"{hub_client.hub_id}_{event.room_id}_{event.channel_id}"

                # Handle cover entities (blinds use level for position)
                if unique_id in hub_client._cover_map:
                    if event.target_level is not None:
                        hub_client._cover_map[unique_id].current_cover_position = event.target_level
                    else:
                        hub_client._cover_map[unique_id].current_cover_position = event.current_level

                # Handle light entities
                if unique_id in hub_client._light_map:
                    if event.target_level is not None:
                        hub_client._light_map[unique_id].brightness = event.target_level
                    else:
                        hub_client._light_map[unique_id].brightness = event.current_level

                # Handle switch entities
                if unique_id in hub_client._switch_map:
                    if event.target_level is not None:
                        hub_client._switch_map[unique_id].level = event.target_level
                    else:
                        hub_client._switch_map[unique_id].level = event.current_level

            elif event and isinstance(event, SceneChangedEvent):
                unique_id = f"{hub_client.hub_id}_{event.room_id}"
                if unique_id in hub_client._scene_map:
                    hub_client._scene_map[unique_id].current_option = event.active_scene_id

        except Exception as e:
            _LOGGER.exception("Unexpected exception: %s", repr(e))
