import asyncio
import logging
import re
from datetime import datetime
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DeyeCloudApiError, DeyeCloudAuthError, async_get_token
from .auth import DeyeCloudSession, normalize_base_url
from .const import (
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_BASE_URL,
    CONF_COMPANY_ID,
    CONF_PASSWORD,
    CONF_START_MONTH,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://eu1-developer.deyecloud.com/v1.0"
DEFAULT_START_MONTH = "2024-01"


def _data_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return config/options schema with defaults."""
    defaults = defaults or {}

    return vol.Schema(
        {
            vol.Required(
                CONF_USERNAME,
                default=defaults.get(CONF_USERNAME, ""),
            ): str,
            vol.Required(
                CONF_PASSWORD,
                default=defaults.get(CONF_PASSWORD, ""),
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_APP_ID,
                default=defaults.get(CONF_APP_ID, ""),
            ): str,
            vol.Required(
                CONF_APP_SECRET,
                default=defaults.get(CONF_APP_SECRET, ""),
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_BASE_URL,
                default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            ): str,
            vol.Required(
                CONF_START_MONTH,
                default=defaults.get(CONF_START_MONTH, DEFAULT_START_MONTH),
            ): str,
            vol.Optional(
                CONF_COMPANY_ID,
                default=defaults.get(CONF_COMPANY_ID, ""),
            ): str,
        }
    )


def _normalize_user_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize form input before saving it to the config entry."""
    data = dict(user_input)

    for key in (
        CONF_USERNAME,
        CONF_APP_ID,
        CONF_BASE_URL,
        CONF_START_MONTH,
        CONF_COMPANY_ID,
    ):
        if key in data and isinstance(data[key], str):
            data[key] = data[key].strip()

    data[CONF_BASE_URL] = normalize_base_url(data[CONF_BASE_URL])

    # Keep old entries clean: do not persist an empty optional company_id.
    if not data.get(CONF_COMPANY_ID):
        data.pop(CONF_COMPANY_ID, None)

    return data


async def _async_validate_credentials_and_stations(hass, user_input: dict[str, Any]) -> None:
    """Validate credentials and ensure at least one station is accessible."""
    session = DeyeCloudSession(async_get_clientsession(hass), user_input)

    token = await async_get_token(
        session,
        user_input[CONF_USERNAME],
        user_input[CONF_PASSWORD],
        user_input[CONF_APP_ID],
        user_input[CONF_APP_SECRET],
        user_input[CONF_BASE_URL],
        user_input.get(CONF_COMPANY_ID),
    )

    station_url = f"{user_input[CONF_BASE_URL]}/station/list"
    headers = {"Authorization": f"Bearer {token}"}

    async with session.post(station_url, headers=headers, json={}, timeout=10) as resp:
        resp.raise_for_status()
        station_response = await resp.json()

    if not station_response.get("success", True):
        raise DeyeCloudApiError("Station list request failed")

    stations = station_response.get("stationList") or []
    if not stations:
        raise NoStationsFound


class NoStationsFound(Exception):
    """Raised when credentials are valid but no stations are accessible."""


def _account_unique_id(data):
    """Preserve the existing identity format for installed entries."""
    return f"{data[CONF_USERNAME]}:{data.get(CONF_COMPANY_ID) or 'personal'}"


def _validate_month(data):
    value = data[CONF_START_MONTH]
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise ValueError("invalid_start_month")
    try:
        month = datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError("invalid_start_month") from exc
    if month.date().replace(day=1) > datetime.now().date().replace(day=1):
        raise ValueError("invalid_start_month")


def _duplicate_account(hass, data, exclude=None):
    identity = _account_unique_id(data)
    return any(
        entry.entry_id != exclude
        and (entry.unique_id == identity or _account_unique_id(entry.data) == identity)
        for entry in hass.config_entries.async_entries(DOMAIN)
    )


async def _async_validate_input(hass, user_input, exclude=None):
    """Normalize and classify form errors without exposing cloud responses."""
    try:
        data = _normalize_user_input(user_input)
    except ValueError:
        return None, "invalid_base_url"
    try:
        _validate_month(data)
    except ValueError:
        return None, "invalid_start_month"
    if _duplicate_account(hass, data, exclude):
        return None, "already_configured"
    try:
        await _async_validate_credentials_and_stations(hass, data)
    except NoStationsFound:
        return None, "no_stations_found"
    except DeyeCloudAuthError:
        return None, "auth_failed"
    except aiohttp.ClientResponseError as exc:
        return None, "auth_failed" if exc.status in (401, 403) else "cannot_connect"
    except (aiohttp.ClientError, asyncio.TimeoutError, DeyeCloudApiError):
        return None, "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error validating DeyeCloud configuration")
        return None, "unknown"
    return data, None


class DeyeCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial configuration and recovery from rejected credentials."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return DeyeCloudOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None:
            data, error = await _async_validate_input(self.hass, user_input)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(_account_unique_id(data))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"DeyeCloud - {data[CONF_USERNAME]}", data=data
                )
        return self.async_show_form(
            step_id="user", data_schema=_data_schema(user_input), errors=errors
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data, error = await _async_validate_input(self.hass, user_input, entry.entry_id)
            if not error and _account_unique_id(data) != _account_unique_id(entry.data):
                error = "account_mismatch"
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_data_schema(user_input or dict(entry.data)),
            errors=errors,
        )


class DeyeCloudOptionsFlowHandler(config_entries.OptionsFlow):
    """Validate changed settings and reload after updating data and identity."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None:
            data, error = await _async_validate_input(
                self.hass, user_input, self._config_entry.entry_id
            )
            if error:
                errors["base"] = error
            else:
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    title=f"DeyeCloud - {data[CONF_USERNAME]}",
                    data=data,
                    unique_id=_account_unique_id(data),
                )
                # Entry data updates trigger the registered update listener.
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="init",
            data_schema=_data_schema(user_input or dict(self._config_entry.data)),
            errors=errors,
        )
