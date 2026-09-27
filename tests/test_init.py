"""Tests for __init__.py (async_setup_entry / async_unload_entry)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import asyncio

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from custom_components.rako import async_setup_entry, async_unload_entry, PLATFORMS
from custom_components.rako.const import DOMAIN
from tests.conftest import MOCK_HOST, MOCK_HUB_ID, MOCK_HUB_MAC, MOCK_NAME, MOCK_USER_INPUT


@pytest.fixture
def mock_entry():
    """Return a minimal mock config entry for __init__ tests."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.data = MOCK_USER_INPUT
    entry.runtime_data = None
    return entry


@pytest.fixture
def mock_hub_client_instance(mock_hub_status):
    """Create a mock HubClient returned by the patched constructor."""
    client = MagicMock()
    client.get_hub_status = AsyncMock(return_value=mock_hub_status)
    return client


async def test_async_setup_entry(
    hass: HomeAssistant, mock_entry, mock_hub_client_instance, mock_hub_status
) -> None:
    """Test that async_setup_entry sets up the integration correctly."""
    mock_device_registry = MagicMock()
    mock_device_registry.async_get_or_create = MagicMock()

    with (
        patch(
            "custom_components.rako.HubClient",
            return_value=mock_hub_client_instance,
        ) as hub_cls,
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as mock_forward,
        patch(
            "custom_components.rako.dr.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await async_setup_entry(hass, mock_entry)

    assert result is True

    # HubClient constructed with the right args
    hub_cls.assert_called_once_with(
        name=MOCK_NAME,
        host=MOCK_HOST,
        entry_id="test_entry_id",
        hass=hass,
    )

    # Hub status fetched
    mock_hub_client_instance.get_hub_status.assert_awaited_once()

    # Device registered
    mock_device_registry.async_get_or_create.assert_called_once_with(
        config_entry_id="test_entry_id",
        connections={(dr.CONNECTION_NETWORK_MAC, MOCK_HUB_MAC)},
        identifiers={(DOMAIN, MOCK_HUB_ID)},
        manufacturer="Rako",
        name="Hub",
    )

    # Runtime data set on entry
    assert mock_entry.runtime_data is not None
    assert mock_entry.runtime_data["hub_id"] == MOCK_HUB_ID
    assert mock_entry.runtime_data["hub_client"] is mock_hub_client_instance

    # Platforms forwarded
    mock_forward.assert_awaited_once_with(mock_entry, PLATFORMS)


async def test_async_unload_entry(hass: HomeAssistant, mock_entry) -> None:
    """Test that async_unload_entry unloads all platforms."""
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_unload:
        result = await async_unload_entry(hass, mock_entry)

    assert result is True
    mock_unload.assert_awaited_once_with(mock_entry, PLATFORMS)


# ---------------------------------------------------------------------------
# Hub not answering at start-up: retry, don't fail for good (27/09/2026)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("refused"),
        OSError("no route to host"),
        ValueError("empty reply from hub"),
        KeyError("payload"),
    ],
)
async def test_setup_not_ready_when_hub_does_not_answer(
    hass: HomeAssistant, mock_entry, mock_hub_client_instance, error
) -> None:
    """A hub that fails at start-up asks Home Assistant to retry later."""
    mock_hub_client_instance.get_hub_status = AsyncMock(side_effect=error)

    with (
        patch("custom_components.rako.HubClient", return_value=mock_hub_client_instance),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as mock_forward,
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, mock_entry)

    mock_hub_client_instance.drop_command_connection.assert_called_once()
    mock_forward.assert_not_awaited()


async def test_setup_not_ready_when_hub_hangs(
    hass: HomeAssistant, mock_entry, mock_hub_client_instance
) -> None:
    """A hub that never answers times out instead of holding start-up."""
    async def hang():
        await asyncio.sleep(3600)

    mock_hub_client_instance.get_hub_status = AsyncMock(side_effect=hang)

    with (
        patch("custom_components.rako.HubClient", return_value=mock_hub_client_instance),
        patch("custom_components.rako.SETUP_TIMEOUT", 0.05),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, mock_entry)
