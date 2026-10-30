"""Regression coverage for data integrity, discovery, and command coordination."""

import ast
import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import test_auth
import test_settings_readback as readback

ROOT = Path(__file__).resolve().parents[1] / "custom_components/deyecloud"


class SensorEntity:
    @property
    def unique_id(self):
        return self._attr_unique_id


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def load_sensor():
    modules = {}
    for name, attrs in {
        "homeassistant.components.sensor": {"SensorEntity": SensorEntity},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.core": {"HomeAssistant": object, "callback": lambda fn: fn},
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
        "homeassistant.exceptions": {
            "ConfigEntryAuthFailed": type("ConfigEntryAuthFailed", (Exception,), {})
        },
        "homeassistant.helpers.update_coordinator": {
            "CoordinatorEntity": readback.CoordinatorEntity,
            "DataUpdateCoordinator": readback.DataUpdateCoordinator,
            "UpdateFailed": RuntimeError,
        },
        "homeassistant.util": {"dt": SimpleNamespace(now=lambda: NOW)},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        modules[name] = module
    with patch.dict(sys.modules, modules):
        return readback.load("sensor"), readback.load("discovery")


sensor, discovery = load_sensor()


class DataIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def coordinator(self):
        hass = MagicMock(
            data={"deyecloud": {"entry": {"discovery": SimpleNamespace(data={"devices": {}})}}}
        )
        entry = SimpleNamespace(
            entry_id="entry", data={**test_auth.CONFIG, "start_month": "2025-01"}
        )
        with patch.object(sensor, "get_api_session", return_value=object()):
            result = sensor.DeyeCloudCoordinator(hass, entry)
        result.token = "token"
        return result

    async def test_history_refresh_is_per_station_and_entry(self):
        coordinator = self.coordinator()
        coordinator._history_cache = {sn: [{"year": 2026, "month": 10}] for sn in ("A", "B")}
        coordinator._history_last_update = {sn: NOW - timedelta(hours=7) for sn in ("A", "B")}
        with patch.object(sensor, "_async_history", AsyncMock(return_value=[])) as history:
            await coordinator._get_monthly_history_cached(None, "A", "base")
            await coordinator._get_monthly_history_cached(None, "B", "base")
        self.assertEqual([call.args[2] for call in history.await_args_list], ["A", "B"])
        self.assertTrue(all(call.args[4] == "2025-01" for call in history.await_args_list))
        self.assertEqual(coordinator._start_month, "2025-01")

    async def test_outage_does_not_manufacture_zero_energy(self):
        coordinator = self.coordinator()
        coordinator.data = {"A": {"daily": {"2026-10-30": {"generationValue": 12}}}}
        with (
            patch.object(sensor, "_async_history", AsyncMock(side_effect=RuntimeError("offline"))),
            patch.object(
                sensor, "_async_daily_history", AsyncMock(side_effect=RuntimeError("offline"))
            ),
        ):
            _, result = await coordinator._async_update_station_data(None, "A", "base", {})
        self.assertEqual(result["history"], [])
        self.assertNotIn("2026-10-30", result["daily"])
        self.assertIsNone(result["daily_read_at"])

    async def test_completed_days_are_cached_and_pruned(self):
        coordinator = self.coordinator()
        records = [{"date": f"2026-10-{day}", "generationValue": day} for day in (28, 29, 30)]
        with (
            patch.object(sensor, "_async_history", AsyncMock(return_value=[])),
            patch.object(sensor, "_async_daily_history", AsyncMock(return_value=records)) as daily,
        ):
            await coordinator._async_update_station_data(None, "A", "base", {})
            _, result = await coordinator._async_update_station_data(None, "A", "base", {})
        self.assertEqual(daily.await_args_list[0].args[-2:], ("2026-10-28", "2026-10-30"))
        self.assertEqual(daily.await_args_list[1].args[-2:], ("2026-10-30", "2026-10-30"))
        self.assertEqual(len(result["daily"]), 3)

    async def test_attributes_follow_latest_device_and_history(self):
        coordinator = self.coordinator()
        coordinator.data = {
            "A": {
                "devices": {"sn": {"collectionTime": 123}},
                "history": [{"year": 2026, "month": 10, "generationValue": 5}],
            }
        }
        entity = sensor.DeyeCloudSensor(
            coordinator, "device", "name", "id", station_id="A", device_sn="sn", device_key="power"
        )
        self.assertEqual(entity.extra_state_attributes["collection_time"], 123)
        coordinator.data["A"]["devices"]["sn"]["collectionTime"] = 456
        self.assertEqual(entity.extra_state_attributes["collection_time"], 456)
        raw = sensor.DeyeCloudSensor(
            coordinator, "monthly_raw", "name", "raw", station_id="A", date_key="2026_10"
        )
        self.assertEqual(raw.extra_state_attributes["generationValue"], 5)
        coordinator.data["A"]["history"][0]["generationValue"] = 9
        self.assertEqual(raw.extra_state_attributes["generationValue"], 9)

    async def test_late_telemetry_adds_entities_once(self):
        coordinator = self.coordinator()
        coordinator.data = {"A": {"history": [], "devices": {}}}
        listeners = []
        coordinator.async_add_listener = lambda fn: listeners.append(fn) or (lambda: None)
        hass = MagicMock(data={"deyecloud": {"entry": {"coordinator": coordinator}}})
        entry = MagicMock(entry_id="entry")
        added = []
        await sensor.async_setup_entry(hass, entry, lambda entities: added.extend(entities))
        count = len(added)
        coordinator.data["A"]["devices"] = {
            "sn": {"dataList": [{"key": "power", "unit": "W", "value": 1}]}
        }
        listeners[0]()
        self.assertGreater(len(added), count)
        count = len(added)
        listeners[0]()
        self.assertEqual(len(added), count)
        self.assertIn("device_sn_power", [entity.unique_id for entity in added])

    async def test_station_pagination_and_repeat_guard(self):
        first = [{"id": n} for n in range(100)]
        with patch.object(
            discovery,
            "async_post_json",
            AsyncMock(
                side_effect=[
                    {"success": True, "stationList": first, "total": 101},
                    {"success": True, "stationList": [{"id": 100}], "total": 101},
                ]
            ),
        ) as post:
            result = await discovery.async_list_all(None, "base", "/station/list", "stationList")
        self.assertEqual(len(result), 101)
        self.assertEqual(post.await_args_list[1].kwargs["payload"]["page"], 2)
        with patch.object(
            discovery,
            "async_post_json",
            AsyncMock(return_value={"success": True, "stationList": first, "total": 201}),
        ):
            with self.assertRaises(RuntimeError):
                await discovery.async_list_all(None, "base", "/station/list", "stationList")


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_status_is_not_success(self):
        api = test_auth.api
        for status in (0, 100, 300, 999):
            self.assertEqual(
                api._classify_order_result({"success": True, "status": status}), "pending"
            )
        self.assertEqual(api._classify_order_result({"success": True, "status": 666}), "success")
        for status in (400, 500):
            self.assertEqual(
                api._classify_order_result({"success": True, "status": status}), "failed"
            )

    async def test_same_device_commands_wait_for_prior_confirmation(self):
        api = test_auth.api
        session = test_auth.auth.DeyeCloudSession(test_auth.Transport(), test_auth.CONFIG)
        events = []

        async def submit(*args, **kwargs):
            events.append("submit")
            return {"orderId": len(events)}

        async def poll(*args, **kwargs):
            events.append("poll-start")
            await asyncio.sleep(0)
            events.append("poll-end")
            return {"status": 666}

        with (
            patch.object(api, "_async_post_control", side_effect=submit),
            patch.object(api, "async_wait_for_order_result", side_effect=poll),
        ):
            await asyncio.gather(
                *(
                    api.async_control_grid_charge(session, "token", test_auth.BASE, "sn", True)
                    for _ in range(2)
                )
            )
        self.assertEqual(events, ["submit", "poll-start", "poll-end"] * 2)
        self.assertEqual(session.pending_orders, {})

    async def test_timeout_is_resolved_before_another_submission(self):
        api = test_auth.api
        session = test_auth.auth.DeyeCloudSession(test_auth.Transport(), test_auth.CONFIG)
        with (
            patch.object(
                api, "_async_post_control", AsyncMock(return_value={"orderId": 123})
            ) as submit,
            patch.object(
                api,
                "async_wait_for_order_result",
                AsyncMock(side_effect=api.DeyeCloudOrderTimeout("pending")),
            ),
        ):
            for _ in range(2):
                with self.assertRaises(api.DeyeCloudOrderTimeout):
                    await api.async_control_grid_charge(
                        session, "token", test_auth.BASE, "sn", True
                    )
        self.assertEqual(submit.await_count, 1)
        self.assertEqual(session.pending_orders, {"sn": 123})

    async def test_throttling_uses_retry_after_without_replaying_commands(self):
        response = test_auth.Response(429)
        response.headers = {"Retry-After": "120"}
        transport = test_auth.Transport([response])
        session = test_auth.auth.DeyeCloudSession(transport, test_auth.CONFIG)
        with patch.object(test_auth.auth, "monotonic", return_value=100):
            async with session.get(test_auth.BASE + "/station/list") as returned:
                self.assertEqual(returned.status, 429)
        self.assertEqual(session._shared["next_request"], 220)
        self.assertEqual(len(transport.calls), 1)

    async def test_unknown_submission_blocks_following_command(self):
        api = test_auth.api
        session = test_auth.auth.DeyeCloudSession(test_auth.Transport(), test_auth.CONFIG)
        with patch.object(
            api, "_async_post_control", AsyncMock(side_effect=TimeoutError)
        ) as submit:
            with self.assertRaises(TimeoutError):
                await api.async_control_grid_charge(session, "token", test_auth.BASE, "sn", True)
            with self.assertRaises(api.DeyeCloudOrderError):
                await api.async_control_grid_charge(session, "token", test_auth.BASE, "sn", False)
        self.assertEqual(submit.await_count, 1)

    async def test_auth_endpoint_http_rejection_is_an_auth_error(self):
        transport = test_auth.Transport()
        transport.post = lambda *args, **kwargs: test_auth.Response(401)
        with self.assertRaises(test_auth.api.DeyeCloudAuthError):
            await test_auth.api.async_get_token(
                transport, "user", "pw", "app", "secret", test_auth.BASE
            )


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def scope(self):
        tree = ast.parse((ROOT / "config_flow.py").read_text())
        functions = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name
            in {
                "_account_unique_id",
                "_validate_month",
                "_duplicate_account",
                "_async_validate_input",
            }
        ]
        import re

        scope = {
            "datetime": datetime,
            "re": re,
            "DOMAIN": "deyecloud",
            "CONF_USERNAME": "username",
            "CONF_COMPANY_ID": "company_id",
            "CONF_START_MONTH": "start_month",
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), "config_flow.py", "exec"), scope)
        return scope

    async def test_invalid_months_and_future_month_rejected(self):
        validate = self.scope()["_validate_month"]
        for month in ("2025-13", "2025-1", "0000-01", "2999-01", "bad"):
            with self.assertRaises(ValueError):
                validate({"start_month": month})
        validate({"start_month": "2024-01"})

    async def test_identity_change_checks_other_entries(self):
        scope = self.scope()
        other = SimpleNamespace(
            entry_id="other", unique_id="user:42", data={"username": "user", "company_id": "42"}
        )
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = [other]
        self.assertTrue(scope["_duplicate_account"](hass, other.data, "current"))
        self.assertFalse(scope["_duplicate_account"](hass, other.data, "other"))


if __name__ == "__main__":
    unittest.main()
