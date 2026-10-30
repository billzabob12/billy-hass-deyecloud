"""Command retry tests; run with python -m unittest discover -s tests -v."""

import asyncio
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp

# Load the API module without importing Home Assistant's integration package.
spec = importlib.util.spec_from_file_location(
    "deyecloud_api",
    Path(__file__).resolve().parents[1] / "custom_components/deyecloud/api.py",
)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def response(status=200, data=None):
    result = MagicMock()
    result.__aenter__ = AsyncMock(return_value=result)
    result.__aexit__ = AsyncMock(return_value=False)
    if status >= 400:
        result.raise_for_status.side_effect = aiohttp.ClientResponseError(
            MagicMock(real_url="https://example.invalid/control"), (), status=status
        )
    result.json = AsyncMock(return_value=data or {"success": True, "orderId": 123})
    return result


class ControlRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = MagicMock()
        self.sleep_patch = patch.object(api.asyncio, "sleep", new_callable=AsyncMock)
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    async def send(self, **kwargs):
        return await api.async_post_control(
            self.session, "test-token", "https://example.invalid", "/control",
            {"action": "on"}, **kwargs
        )

    async def test_success_without_retry(self):
        self.session.post.return_value = response()
        result = await self.send(wait_for_result=False)
        self.assertEqual(result["orderId"], 123)
        self.session.post.assert_called_once()
        self.sleep.assert_not_awaited()

    async def test_recovers_after_two_server_errors(self):
        self.session.post.side_effect = [response(500), response(500), response()]
        with patch.object(api, "async_wait_for_order_result", new_callable=AsyncMock) as poll:
            poll.return_value = {"status": "SUCCESS"}
            result = await self.send()
            poll.assert_awaited_once()
        self.assertEqual(result["order_result"], {"status": "SUCCESS"})
        self.assertEqual(self.session.post.call_count, 3)
        self.assertTrue(all(c == self.session.post.call_args for c in self.session.post.call_args_list))
        self.assertEqual(self.sleep.await_args_list, [call(2), call(4)])

    async def test_stops_after_three_server_errors(self):
        self.session.post.side_effect = [response(500) for _ in range(3)]
        with self.assertRaises(aiohttp.ClientResponseError):
            await self.send()
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(self.sleep.await_count, 2)

    async def test_other_http_errors_are_not_retried(self):
        for status in (400, 401, 403, 429, 502, 503):
            with self.subTest(status=status):
                self.session.post.reset_mock()
                self.session.post.return_value = response(status)
                with self.assertRaises(aiohttp.ClientResponseError):
                    await self.send()
                self.session.post.assert_called_once()
        self.sleep.assert_not_awaited()

    async def test_timeout_is_not_retried(self):
        self.session.post.side_effect = asyncio.TimeoutError()
        with self.assertRaises(asyncio.TimeoutError):
            await self.send()
        self.session.post.assert_called_once()

    async def test_poll_error_does_not_resubmit_accepted_order(self):
        self.session.post.return_value = response()
        with patch.object(api, "async_wait_for_order_result", new_callable=AsyncMock) as poll:
            poll.side_effect = aiohttp.ClientResponseError(MagicMock(), (), status=500)
            with self.assertRaises(aiohttp.ClientResponseError):
                await self.send()
        self.session.post.assert_called_once()
        self.sleep.assert_not_awaited()

    async def test_api_rejection_is_not_retried(self):
        self.session.post.return_value = response(data={"success": False, "msg": "rejected"})
        with self.assertRaises(api.DeyeCloudOrderError):
            await self.send()
        self.session.post.assert_called_once()

    async def test_cancellation_stops_retries(self):
        self.session.post.return_value = response(500)
        self.sleep.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.send()
        self.session.post.assert_called_once()

    async def test_parameter_failure_does_not_start_another_retry_batch(self):
        self.session.post.side_effect = [response(500) for _ in range(3)]
        with self.assertRaises(aiohttp.ClientResponseError):
            await api.async_update_battery_parameter(
                self.session, "test-token", "https://example.invalid", "test-device",
                "GRID_CHARGE_AMPERE", 190, wait_for_result=False,
            )
        calls = self.session.post.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("paramterType" in c.kwargs["json"] for c in calls))


if __name__ == "__main__":
    unittest.main()
