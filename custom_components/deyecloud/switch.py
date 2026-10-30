"""DeyeCloud switch entities."""

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import async_control_grid_charge, async_get_token
from .auth import get_api_session
from .const import (
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_BASE_URL,
    CONF_COMPANY_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)
from .discovery import async_watch_inverters
from .settings import async_get_settings_coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DeyeCloud grid charge switches."""
    config = entry.data

    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    app_id = config.get(CONF_APP_ID)
    app_secret = config.get(CONF_APP_SECRET)
    base_url = config.get(CONF_BASE_URL)
    company_id = config.get(CONF_COMPANY_ID)

    session = get_api_session(hass, entry)

    async def factory(sn):
        coordinator = await async_get_settings_coordinator(hass, entry, sn)
        return [
            DeyeGridChargeSwitch(
                hass,
                username,
                password,
                app_id,
                app_secret,
                base_url,
                company_id,
                sn,
                coordinator,
                session,
            )
        ]

    await async_watch_inverters(hass, entry, async_add_entities, factory)


class DeyeGridChargeSwitch(CoordinatorEntity, SwitchEntity):
    """DeyeCloud grid charge switch."""

    def __init__(
        self,
        hass,
        username,
        password,
        app_id,
        app_secret,
        base_url,
        company_id,
        device_sn,
        coordinator,
        session,
    ):
        super().__init__(coordinator)
        self.hass = hass
        self._session = session
        self._username = username
        self._password = password
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url
        self._company_id = company_id
        self._device_sn = device_sn

        self._attr_name = f"Deye Grid Charge {device_sn}"
        self._attr_unique_id = f"{device_sn}_grid_charge"
        self._attr_icon = "mdi:battery-charging"
        self._attr_assumed_state = False

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._device_sn)},
            "name": f"Deye Inverter {self._device_sn}",
            "manufacturer": "Deye",
            "model": "Inverter",
        }

    @property
    def is_on(self):
        """Return grid charge state."""
        return (self.coordinator.data or {}).get("grid_charge")

    @property
    def available(self):
        return super().available and self.is_on is not None

    @property
    def extra_state_attributes(self):
        return {
            "state_source": "DeyeCloud settings readback",
            "settings_read_at": self.coordinator.read_at,
        }

    async def _async_send(self, enable: bool) -> None:
        """Send grid charge command and wait for DeyeCloud confirmation."""
        session = self._session

        try:
            token = await async_get_token(
                session,
                self._username,
                self._password,
                self._app_id,
                self._app_secret,
                self._base_url,
                self._company_id,
            )

            response = await async_control_grid_charge(
                session,
                token,
                self._base_url,
                self._device_sn,
                enable,
                wait_for_result=True,
            )

            _LOGGER.info(
                "Grid charge %s confirmed for device %s: %s",
                "enable" if enable else "disable",
                self._device_sn,
                response,
            )

            await self.coordinator.async_refresh()

        except Exception as exc:
            raise HomeAssistantError(
                f"Failed to {'enable' if enable else 'disable'} "
                f"Deye grid charge for {self._device_sn}: {exc}"
            ) from exc

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on grid charge."""
        await self._async_send(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off grid charge."""
        await self._async_send(False)
