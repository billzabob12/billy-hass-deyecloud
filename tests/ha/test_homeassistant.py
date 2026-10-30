"""Exercise real HA config flows and setup/unload interfaces without the cloud."""

import tempfile
import unittest
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components import deyecloud
from custom_components.deyecloud import config_flow, discovery, sensor

CONFIG = {
    "username": "user",
    "password": "pw",
    "app_id": "app",
    "app_secret": "secret",
    "base_url": "https://eu1-developer.deyecloud.com/v1.0",
    "start_month": "2024-01",
}


class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.entry = ConfigEntry(
            state=ConfigEntryState.SETUP_IN_PROGRESS,
            data=CONFIG,
            discovery_keys=MappingProxyType({}),
            domain="deyecloud",
            minor_version=1,
            options={},
            source="user",
            title="Deye",
            unique_id="user:personal",
            version=1,
        )
        self.hass.config_entries._entries[self.entry.entry_id] = self.entry

    async def asyncTearDown(self):
        await self.entry._async_process_on_unload(self.hass)
        await self.hass.async_stop()
        self.directory.cleanup()

    async def test_setup_refreshes_before_forwarding_and_unload_cleans_runtime(self):
        stations = {"stations": [{"id": 1}], "devices": {"1": []}}
        with (
            patch.object(deyecloud, "_async_preload_platforms", AsyncMock()),
            patch.object(sensor, "get_api_session", return_value=object()),
            patch.object(
                discovery.DeyeDiscoveryCoordinator,
                "_async_update_data",
                AsyncMock(return_value=stations),
            ),
            patch.object(
                sensor.DeyeCloudCoordinator,
                "_async_update_data",
                AsyncMock(return_value={"1": {"devices": {}}}),
            ),
            patch.object(
                self.hass.config_entries, "async_forward_entry_setups", AsyncMock()
            ) as forward,
            patch.object(
                self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
            ),
        ):
            self.assertTrue(await deyecloud.async_setup_entry(self.hass, self.entry))
            runtime = self.hass.data["deyecloud"][self.entry.entry_id]
            self.assertEqual(runtime["discovery"].data, stations)
            self.assertIsNotNone(runtime["coordinator"].data)
            forward.assert_awaited_once()
            self.assertTrue(await deyecloud.async_unload_entry(self.hass, self.entry))
            self.assertNotIn(self.entry.entry_id, self.hass.data["deyecloud"])

    async def test_startup_outage_raises_retry_before_platforms(self):
        with (
            patch.object(deyecloud, "_async_preload_platforms", AsyncMock()),
            patch.object(sensor, "get_api_session", return_value=object()),
            patch.object(
                discovery.DeyeDiscoveryCoordinator,
                "_async_update_data",
                AsyncMock(side_effect=UpdateFailed("offline")),
            ),
            patch.object(
                self.hass.config_entries, "async_forward_entry_setups", AsyncMock()
            ) as forward,
        ):
            with self.assertRaises(ConfigEntryNotReady):
                await deyecloud.async_setup_entry(self.hass, self.entry)
            forward.assert_not_awaited()

    async def test_user_flow_distinguishes_network_auth_and_invalid_month(self):
        import aiohttp

        from custom_components.deyecloud.api import DeyeCloudAuthError

        for exc, error in [
            (aiohttp.ClientConnectionError(), "cannot_connect"),
            (DeyeCloudAuthError(), "auth_failed"),
        ]:
            flow = config_flow.DeyeCloudConfigFlow()
            flow.hass = self.hass
            data = {**CONFIG, "username": "another"}
            with patch.object(
                config_flow, "_async_validate_credentials_and_stations", AsyncMock(side_effect=exc)
            ):
                result = await flow.async_step_user(data)
            self.assertEqual(result["errors"]["base"], error)
        flow = config_flow.DeyeCloudConfigFlow()
        flow.hass = self.hass
        result = await flow.async_step_user({**CONFIG, "start_month": "bad"})
        self.assertEqual(result["errors"]["base"], "invalid_start_month")

    async def test_options_updates_identity_and_rejects_duplicate(self):
        flow = config_flow.DeyeCloudOptionsFlowHandler(self.entry)
        flow.hass = self.hass
        with patch.object(config_flow, "_async_validate_credentials_and_stations", AsyncMock()):
            result = await flow.async_step_init({**CONFIG, "username": "changed"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.entry.unique_id, "changed:personal")
        self.assertEqual(self.entry.data["username"], "changed")
        result = await config_flow._async_validate_input(
            self.hass, {**CONFIG, "username": "changed"}
        )
        self.assertEqual(result[1], "already_configured")

    async def test_reauth_preserves_account_and_updates_password(self):
        flow = config_flow.DeyeCloudConfigFlow()
        flow.hass = self.hass
        flow.context = {"entry_id": self.entry.entry_id, "source": "reauth"}
        with (
            patch.object(config_flow, "_async_validate_credentials_and_stations", AsyncMock()),
            patch.object(self.hass.config_entries, "async_reload", AsyncMock(return_value=True)),
        ):
            result = await flow.async_step_reauth_confirm({**CONFIG, "password": "new password"})
            await self.hass.async_block_till_done()
        self.assertEqual(result["reason"], "reauth_successful")
        self.assertEqual(self.entry.data["password"], "new password")


if __name__ == "__main__":
    unittest.main()
