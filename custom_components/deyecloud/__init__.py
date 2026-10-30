"""DeyeCloud integration."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.loader import async_get_loaded_integration

from .const import DOMAIN
from .discovery import DeyeDiscoveryCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.NUMBER,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the DeyeCloud integration."""
    return True


async def _async_preload_platforms(
    hass: HomeAssistant,
    platforms: Iterable[Platform],
) -> None:
    """
    Preload platform modules through Home Assistant's loader.

    This avoids Home Assistant later importing custom_components.deyecloud.sensor,
    button, switch, or number inside the event loop during async_forward_entry_setups.

    Do not import platform modules at the top of this file. That can put the
    modules in sys.modules without adding them to Home Assistant's loader cache,
    which may still trigger an import_module warning during platform forwarding.
    """
    integration = async_get_loaded_integration(hass, DOMAIN)

    platform_names = [platform.value for platform in platforms]

    try:
        await integration.async_get_platforms(platform_names)
    except Exception:
        _LOGGER.exception("Failed to preload DeyeCloud platforms")
        raise


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DeyeCloud from a config entry."""
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {}

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await _async_preload_platforms(hass, PLATFORMS)

    # Validate and discover before forwarding platforms, so HA retries startup
    # failures instead of silently marking individual platforms as loaded.
    from .sensor import DeyeCloudCoordinator

    runtime = hass.data[DOMAIN].setdefault(entry.entry_id, {})
    discovery = runtime["discovery"] = DeyeDiscoveryCoordinator(hass, entry)
    await discovery.async_config_entry_first_refresh()
    coordinator = runtime["coordinator"] = DeyeCloudCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.async_on_unload(
        discovery.async_add_listener(
            lambda: hass.async_create_task(coordinator.async_request_refresh())
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a DeyeCloud config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
