"""DeyeCloud button entities."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import async_control_solar_sell, async_get_token
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

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solar Sell buttons dynamically."""
    config = entry.data

    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    app_id = config.get(CONF_APP_ID)
    app_secret = config.get(CONF_APP_SECRET)
    base_url = config.get(CONF_BASE_URL)
    company_id = config.get(CONF_COMPANY_ID)

    session = get_api_session(hass, entry)

    async def factory(sn):
        return [
            DeyeSolarSellButton(
                hass,
                username,
                password,
                app_id,
                app_secret,
                base_url,
                company_id,
                sn,
                "Enable",
                True,
                "mdi:solar-power",
                session,
            ),
            DeyeSolarSellButton(
                hass,
                username,
                password,
                app_id,
                app_secret,
                base_url,
                company_id,
                sn,
                "Disable",
                False,
                "mdi:solar-power-variant-outline",
                session,
            ),
        ]

    await async_watch_inverters(hass, entry, async_add_entities, factory)


class DeyeSolarSellButton(ButtonEntity):
    """DeyeCloud Solar Sell control button."""

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
        action_name,
        is_enable,
        icon,
        session,
    ):
        self.hass = hass
        self._session = session
        self._username = username
        self._password = password
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url
        self._company_id = company_id
        self._device_sn = device_sn
        self._is_enable = is_enable

        self._attr_name = f"Deye Solar Sell {action_name} {device_sn}"
        self._attr_unique_id = f"{device_sn}_solar_sell_{action_name.lower()}_btn"
        self._attr_icon = icon

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._device_sn)},
            "name": f"Deye Inverter {self._device_sn}",
            "manufacturer": "Deye",
            "model": "Inverter",
        }

    async def async_press(self) -> None:
        """Handle button press."""
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

            response = await async_control_solar_sell(
                session,
                token,
                self._base_url,
                self._device_sn,
                self._is_enable,
                wait_for_result=True,
            )

            _LOGGER.info(
                "Solar Sell %s confirmed for device %s: %s",
                "enable" if self._is_enable else "disable",
                self._device_sn,
                response,
            )

        except Exception as exc:
            raise HomeAssistantError(
                f"Failed to {'enable' if self._is_enable else 'disable'} "
                f"Deye Solar Sell for {self._device_sn}: {exc}"
            ) from exc
