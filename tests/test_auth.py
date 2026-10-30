"""Authentication regression tests; run with python -m unittest discover -s tests.

The HTTP boundary is faked; these tests need aiohttp but no running HA instance.
"""

import ast
import asyncio
import hashlib
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "deyecloud"
package = types.ModuleType("deye_auth_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
api = importlib.import_module("deye_auth_tests.api")
auth = importlib.import_module("deye_auth_tests.auth")
BASE = "https://eu1-developer.deyecloud.com/v1.0"
CONFIG = dict(
    username="user",
    password=" password ",
    app_id="app",
    app_secret="secret",
    base_url=BASE,
    company_id="42",
)


class Response:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data if data is not None else {"success": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.data

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)


class Transport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.logins = []

    def post(self, url, **kwargs):
        self.logins.append((url, kwargs))
        return Response(data={"success": True, "accessToken": f"token{len(self.logins)}"})

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.copy()))
        return self.responses.pop(0) if self.responses else Response()


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_platforms_share_one_login(self):
        transport = Transport()
        session = auth.DeyeCloudSession(transport, CONFIG)
        tokens = await asyncio.gather(*(session.async_get_token() for _ in range(20)))
        self.assertEqual(set(tokens), {"token1"})
        self.assertEqual(len(transport.logins), 1)
        session._expires = 0
        self.assertEqual(await session.async_get_token(), "token2")

    async def test_accounts_have_separate_caches(self):
        transport = Transport()
        one = auth.DeyeCloudSession(transport, CONFIG)
        two = auth.DeyeCloudSession(transport, {**CONFIG, "company_id": "99"})
        self.assertNotEqual(await one.async_get_token(), await two.async_get_token())
        self.assertEqual(transport.logins[1][1]["json"]["companyId"], "99")

    async def test_password_hash_and_login_payload(self):
        transport = Transport()
        session = auth.DeyeCloudSession(transport, {**CONFIG, "username": " user@example.com "})
        await session.async_get_token()
        url, kwargs = transport.logins[0]
        self.assertEqual(url, BASE + "/account/token?appId=app")
        self.assertEqual(
            kwargs["json"],
            {
                "email": "user@example.com",
                "password": hashlib.sha256(b" password ").hexdigest(),
                "appSecret": "secret",
                "companyId": "42",
            },
        )
        self.assertFalse(kwargs["allow_redirects"])

    async def test_rejected_token_is_renewed_once(self):
        for status in (401, 403):
            transport = Transport([Response(status), Response()])
            session = auth.DeyeCloudSession(transport, CONFIG)
            async with session.post(BASE + "/station/list", json={}) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(len(transport.logins), 2)
            self.assertEqual(
                [c[2]["headers"]["Authorization"] for c in transport.calls],
                ["Bearer token1", "Bearer token2"],
            )

    async def test_persistent_rejection_is_bounded(self):
        transport = Transport([Response(401), Response(401)])
        session = auth.DeyeCloudSession(transport, CONFIG)
        with self.assertRaises(api.DeyeCloudAuthError):
            async with session.get(BASE + "/order/123") as response:
                response.raise_for_status()
        self.assertEqual(len(transport.calls), 2)

    async def test_gateway_failure_does_not_replay_command(self):
        transport = Transport([Response(502)])
        session = auth.DeyeCloudSession(transport, CONFIG)
        with self.assertRaises(aiohttp.ClientResponseError):
            await api.async_control_grid_charge(session, "stale", BASE, "sn", True)
        self.assertEqual(len(transport.calls), 1)

    async def test_network_timeout_does_not_replay_command(self):
        transport = Transport()
        session = auth.DeyeCloudSession(transport, CONFIG)

        def timeout(*args, **kwargs):
            transport.calls.append((args, kwargs))
            raise asyncio.TimeoutError

        transport.request = timeout
        with self.assertRaises(asyncio.TimeoutError):
            await api.async_control_grid_charge(session, "stale", BASE, "sn", True)
        self.assertEqual(len(transport.calls), 1)

    async def test_order_poll_renews_rejected_token(self):
        transport = Transport(
            [
                Response(data={"success": True, "orderId": "123"}),
                Response(401),
                Response(data={"success": True, "status": "SUCCESS"}),
            ]
        )
        session = auth.DeyeCloudSession(transport, CONFIG)
        result = await api.async_control_grid_charge(session, "stale", BASE, "sn", True)
        self.assertEqual(result["order_result"]["status"], "SUCCESS")
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET", "GET"])

    async def test_failed_login_is_not_cached(self):
        transport = Transport()
        session = auth.DeyeCloudSession(transport, CONFIG)
        with patch.object(auth, "async_get_token", new_callable=AsyncMock) as login:
            login.side_effect = [api.DeyeCloudApiError("rejected"), "new-token"]
            with self.assertRaises(api.DeyeCloudApiError):
                await session.async_get_token()
            self.assertEqual(await session.async_get_token(), "new-token")
            self.assertEqual(login.await_count, 2)

    async def test_redirect_is_not_followed(self):
        transport = Transport([Response(302)])
        session = auth.DeyeCloudSession(transport, CONFIG)
        with self.assertRaises(ValueError):
            async with session.post(BASE + "/station/list"):
                self.fail("Redirect response must not be yielded")
        self.assertFalse(transport.calls[0][2]["allow_redirects"])

    async def test_cross_host_request_fails_before_login(self):
        transport = Transport()
        session = auth.DeyeCloudSession(transport, CONFIG)
        with self.assertRaises(ValueError):
            async with session.post("https://example.com/station/list"):
                pass
        self.assertFalse(transport.logins)

    async def test_missing_token_and_redirect_rejected(self):
        for response in (Response(data={"success": True}), Response(302)):
            transport = Transport()
            transport.post = lambda *a, **k: response
            with self.assertRaises(api.DeyeCloudApiError):
                await api.async_get_token(transport, "user", "pw", "app", "secret", BASE)

    async def test_failed_login_does_not_echo_response_secrets(self):
        transport = Transport()
        transport.post = lambda *a, **k: Response(data={"success": False, "msg": "secret"})
        with self.assertRaises(api.DeyeCloudApiError) as error:
            await api.async_get_token(transport, "user", "pw", "app", "secret", BASE)
        self.assertNotIn("secret", str(error.exception))

    async def test_order_timeout_does_not_resubmit_parameter_command(self):
        with patch.object(api, "async_post_control", new_callable=AsyncMock) as send:
            send.side_effect = api.DeyeCloudOrderTimeout("uncertain")
            with self.assertRaises(api.DeyeCloudOrderTimeout):
                await api.async_update_battery_parameter(
                    None, "token", BASE, "sn", "MAX_CHARGE_CURRENT", 10
                )
            self.assertEqual(send.await_count, 1)


class InputTests(unittest.TestCase):
    def test_only_documented_https_hosts_are_accepted(self):
        self.assertEqual(auth.normalize_base_url(BASE + "/"), BASE)
        for value in (
            BASE.replace("https:", "http:"),
            BASE + "?x=1",
            BASE + "#x",
            BASE.replace(".com", ".com.evil.test"),
            BASE.replace("https://", "https://user@"),
            BASE.replace(".com", ".com:443"),
            "https://example.com/v1.0",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                auth.normalize_base_url(value)

    def test_normalization_preserves_secret_whitespace(self):
        # Isolate the pure normalization function from HA's UI dependencies.
        tree = ast.parse((ROOT / "config_flow.py").read_text())
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_normalize_user_input"
        )
        const = importlib.import_module("deye_auth_tests.const")
        scope = {**vars(const), "Any": object, "normalize_base_url": auth.normalize_base_url}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "config_flow.py", "exec"), scope)
        value = scope[fn.name]({**CONFIG, "username": " user ", "app_secret": " secret "})
        self.assertEqual(value["password"], " password ")
        self.assertEqual(value["app_secret"], " secret ")
        self.assertEqual(value["username"], "user")


if __name__ == "__main__":
    unittest.main()
