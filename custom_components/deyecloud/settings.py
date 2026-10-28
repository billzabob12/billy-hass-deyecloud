"""Shared per-inverter DeyeCloud settings readback."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import async_get_battery_config, async_get_token, async_post_control
from .const import (
    CONF_APP_ID, CONF_APP_SECRET, CONF_BASE_URL, CONF_COMPANY_ID,
    CONF_PASSWORD, CONF_USERNAME, DOMAIN,
)
from .settings_values import decode_settings

_LOGGER = logging.getLogger(__name__)


class DeyeSettingsCoordinator(DataUpdateCoordinator):
    """Read settings once per inverter for all its control entities."""

    def __init__(self, hass, entry, device_sn):
        super().__init__(hass, _LOGGER, name=f"Deye settings {device_sn}",
                         update_interval=timedelta(minutes=5))
        self.entry = entry
        self.device_sn = device_sn
        self._token = None
        self._token_expiry = datetime.min.replace(tzinfo=timezone.utc)
        self.read_at = None
        self.setup_lock = asyncio.Lock()
        self.initialized = False

    async def _async_update_data(self):
        config = self.entry.data
        session = async_get_clientsession(self.hass)
        now = datetime.now(timezone.utc)
        try:
            if not self._token or now >= self._token_expiry:
                self._token = await async_get_token(
                    session, config[CONF_USERNAME], config[CONF_PASSWORD],
                    config[CONF_APP_ID], config[CONF_APP_SECRET],
                    config[CONF_BASE_URL], config.get(CONF_COMPANY_ID),
                )
                self._token_expiry = now + timedelta(minutes=25)
            battery, dynamic = await asyncio.gather(
                async_get_battery_config(
                    session, self._token, config[CONF_BASE_URL], self.device_sn
                ),
                async_post_control(
                    session, self._token, config[CONF_BASE_URL],
                    "/strategy/dynamicControl/read", {"deviceSn": self.device_sn},
                ),
                return_exceptions=True,
            )
            if isinstance(battery, Exception):
                _LOGGER.debug("Battery settings read failed for %s: %s", self.device_sn, battery)
                battery = {}
            if isinstance(dynamic, Exception):
                _LOGGER.debug("Dynamic settings read failed for %s: %s", self.device_sn, dynamic)
                dynamic = {}
            settings = decode_settings(battery, dynamic.get("order_result") or {})
            if not settings:
                raise UpdateFailed("DeyeCloud returned no supported settings readback")
            self.read_at = datetime.now(timezone.utc).isoformat()
            return settings
        except Exception as exc:
            # Reauthenticate next cycle after any failure; never infer values.
            self._token = None
            raise UpdateFailed(f"DeyeCloud settings read failed: {exc}") from exc


async def async_get_settings_coordinator(hass, entry, device_sn):
    """Get a shared coordinator, retaining entities if initial readback fails."""
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    coordinators = entry_data.setdefault("settings_coordinators", {})
    key = str(device_sn)
    if key not in coordinators:
        coordinators[key] = DeyeSettingsCoordinator(hass, entry, key)
    coordinator = coordinators[key]
    async with coordinator.setup_lock:
        if not coordinator.initialized:
            await coordinator.async_refresh()
            coordinator.initialized = True
    return coordinator
