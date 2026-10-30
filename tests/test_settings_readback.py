"""Readback decoding and entity behavior without a running Home Assistant."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components/deyecloud"
PKG = "deye_readback_test"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package


def load(name):
    fullname = f"{PKG}.{name}"
    if fullname in sys.modules:
        return sys.modules[fullname]
    spec = importlib.util.spec_from_file_location(fullname, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    spec.loader.exec_module(module)
    return module


values = load("settings_values")


class DecodeTests(unittest.TestCase):
    def test_documented_battery_fields_and_zero(self):
        self.assertEqual(
            values.decode_settings(
                {"success": True, "maxChargeCurrent": 190, "maxDischargeCurrent": 0}, {}
            ),
            {"max_battery_charge_current": 190, "max_battery_discharge_current": 0},
        )

    def test_dynamic_read_json_and_supported_layout(self):
        self.assertEqual(
            values.decode_settings(
                {},
                {"analysisResult": '{"00F4":2,"00F8":1,"00E8":0,"00E6":190,"00D2":190,"00D3":0}'},
            ),
            {
                "grid_charge": False,
                "grid_charge_current": 190,
                "max_battery_charge_current": 190,
                "max_battery_discharge_current": 0,
            },
        )

    def test_unknown_layout_does_not_guess(self):
        self.assertEqual(values.decode_settings({}, {"analysisResult": {"00E8": 1}}), {})

    def test_invalid_data_does_not_become_off_or_zero(self):
        for raw in (None, "garbage", [], '{"raw":"unrecognised"}'):
            self.assertEqual(values.decode_settings({}, {"analysisResult": raw}), {})
        self.assertEqual(
            values.decode_settings(
                {}, {"analysisResult": {"00F4": 2, "00F8": 1, "00E8": 2, "00E6": "NaN", "00D2": -1}}
            ),
            {},
        )

    def test_decoded_endpoint_takes_precedence(self):
        result = values.decode_settings(
            {"maxChargeCurrent": 100},
            {"analysisResult": {"00F4": 2, "00F8": 1, "00D2": 190, "00E8": 1}},
        )
        self.assertEqual(result["max_battery_charge_current"], 100)
        self.assertIs(result["grid_charge"], True)


# Minimal HA interfaces, only for exercising our entity/coordinator logic.
# Real HA lifecycle and cloud-device compatibility require installation testing.
class Entity:
    pass


class CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def available(self):
        return self.coordinator.last_update_success


class DataUpdateCoordinator:
    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.data = None
        self.last_update_success = True

    async def async_refresh(self):
        try:
            self.data = await self._async_update_data()
            self.last_update_success = True
        except Exception:
            self.last_update_success = False


def load_platforms():
    modules = {
        "homeassistant.components.switch": {"SwitchEntity": Entity},
        "homeassistant.components.number": {"NumberEntity": Entity},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.exceptions": {
            "HomeAssistantError": RuntimeError,
            "ConfigEntryAuthFailed": type("ConfigEntryAuthFailed", (Exception,), {}),
        },
        "homeassistant.helpers.aiohttp_client": {"async_get_clientsession": lambda hass: object()},
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
        "homeassistant.helpers.update_coordinator": {
            "CoordinatorEntity": CoordinatorEntity,
            "DataUpdateCoordinator": DataUpdateCoordinator,
            "UpdateFailed": RuntimeError,
        },
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module
    button = types.ModuleType(f"{PKG}.button")
    button._async_fetch_inverter_devices = AsyncMock()
    button._async_fetch_station_list = AsyncMock()
    modules[button.__name__] = button
    with patch.dict(sys.modules, modules):
        return load("settings"), load("switch"), load("number")


settings, switches, numbers = load_platforms()


class ReadbackTests(unittest.IsolatedAsyncioTestCase):
    def coordinator(self):
        entry = MagicMock(
            data={
                "username": "user",
                "password": "pass",
                "app_id": "id",
                "app_secret": "secret",
                "base_url": "https://example.invalid",
            }
        )
        return settings.DeyeSettingsCoordinator(MagicMock(), entry, "device")

    async def test_partial_read_drops_missing_settings(self):
        coordinator = self.coordinator()
        coordinator.data = {"grid_charge": True, "grid_charge_current": 190}
        with (
            patch.object(settings, "get_api_session", return_value=object()),
            patch.object(settings, "async_get_token", AsyncMock(return_value="token")),
            patch.object(
                settings,
                "async_get_battery_config",
                AsyncMock(return_value={"maxChargeCurrent": 100}),
            ),
            patch.object(
                settings, "async_post_control", AsyncMock(side_effect=RuntimeError("offline"))
            ),
        ):
            await coordinator.async_refresh()
        self.assertEqual(coordinator.data, {"max_battery_charge_current": 100})
        self.assertTrue(coordinator.last_update_success)
        self.assertIsNotNone(coordinator.read_at)

    async def test_total_read_failure_makes_entities_unavailable(self):
        coordinator = self.coordinator()
        coordinator.data = {"grid_charge": True}
        with (
            patch.object(settings, "get_api_session", return_value=object()),
            patch.object(
                settings, "async_get_token", AsyncMock(side_effect=RuntimeError("offline"))
            ),
        ):
            await coordinator.async_refresh()
        switch = switches.DeyeGridChargeSwitch(
            MagicMock(), "u", "p", "a", "s", "url", None, "sn", coordinator, object()
        )
        self.assertFalse(switch.available)

    async def test_switch_does_not_echo_requested_value(self):
        coordinator = MagicMock(data={"grid_charge": False}, last_update_success=True)
        coordinator.async_refresh = AsyncMock()
        switch = switches.DeyeGridChargeSwitch(
            MagicMock(), "u", "p", "a", "s", "url", None, "sn", coordinator, object()
        )
        with (
            patch.object(switches, "async_get_token", AsyncMock(return_value="token")),
            patch.object(
                switches, "async_control_grid_charge", AsyncMock(return_value={"success": True})
            ),
        ):
            await switch.async_turn_on()
        self.assertIs(switch.is_on, False)
        coordinator.async_refresh.assert_awaited_once()
        coordinator.data = {"grid_charge": True}
        self.assertIs(switch.is_on, True)
        coordinator.data = {}
        self.assertFalse(switch.available)

    async def test_number_reads_cloud_instead_of_requested_value(self):
        coordinator = MagicMock(data={"grid_charge_current": 0}, last_update_success=True)
        coordinator.async_refresh = AsyncMock()
        number = numbers.DeyeBatteryCurrentNumber(
            MagicMock(),
            "u",
            "p",
            "a",
            "s",
            "url",
            None,
            "sn",
            numbers.NUMBER_DESCRIPTIONS[2],
            coordinator,
            object(),
        )
        with (
            patch.object(numbers, "async_get_token", AsyncMock(return_value="token")),
            patch.object(
                numbers, "async_update_battery_parameter", AsyncMock(return_value={"success": True})
            ),
        ):
            await number.async_set_native_value(190)
        self.assertEqual(number.native_value, 0)
        coordinator.async_refresh.assert_awaited_once()
        coordinator.data = {"grid_charge_current": 190}
        self.assertEqual(number.native_value, 190)
        coordinator.data = {}
        self.assertFalse(number.available)

    async def test_device_settings_coordinator_is_shared(self):
        hass = MagicMock(data={})
        entry = MagicMock(entry_id="entry", data={})
        with patch.object(
            settings.DeyeSettingsCoordinator, "async_refresh", AsyncMock()
        ) as refresh:
            first = await settings.async_get_settings_coordinator(hass, entry, "sn")
            second = await settings.async_get_settings_coordinator(hass, entry, "sn")
        self.assertIs(first, second)
        refresh.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
