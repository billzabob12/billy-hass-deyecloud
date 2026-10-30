"""Per-entry authentication shared by all DeyeCloud platforms."""

import asyncio
from contextlib import asynccontextmanager
from time import monotonic
from urllib.parse import urlsplit

from .api import DeyeCloudAuthError, async_get_token


def normalize_base_url(value: str) -> str:
    """Only send credentials to official HTTPS developer endpoints."""
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"eu1-developer.deyecloud.com", "us1-developer.deyecloud.com"}
        or parsed.netloc != parsed.hostname
        or parsed.path != "/v1.0"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use an official DeyeCloud HTTPS developer URL ending in /v1.0")
    return value


class DeyeCloudSession:
    """Wrap HA's shared HTTP session with an isolated, locked token cache.

    Only explicitly rejected requests are replayed, once. Network failures and
    ambiguous command results must never cause automatic command resubmission.
    """

    def __init__(self, session, config, shared=None):
        self._session = session
        self._config = dict(config)
        self._base_url = normalize_base_url(config["base_url"])
        self._token = None
        self._expires = 0
        self._lock = asyncio.Lock()
        self._shared = shared if shared is not None else {}
        self._request_lock = self._shared.setdefault("request_lock", asyncio.Lock())
        self._command_locks = self._shared.setdefault("command_locks", {})
        self.pending_orders = self._shared.setdefault("pending_orders", {})
        self._interval = 1.1 if shared is not None else 0

    def command_lock(self, device_sn):
        """Share command serialization across entries for the same API app."""
        return self._command_locks.setdefault(device_sn, asyncio.Lock())

    async def _async_wait_for_request(self):
        async with self._request_lock:
            delay = self._shared.get("next_request", 0) - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._shared["next_request"] = monotonic() + self._interval

    async def async_get_token(self):
        async with self._lock:
            if self._token and monotonic() < self._expires:
                return self._token
            config = self._config
            self._token = await async_get_token(
                self._session,
                config["username"],
                config["password"],
                config["app_id"],
                config["app_secret"],
                self._base_url,
                config.get("company_id"),
            )
            # Conservative local lifetime; rejected tokens are renewed immediately.
            self._expires = monotonic() + 25 * 60
            return self._token

    @asynccontextmanager
    async def _request(self, method, url, **kwargs):
        if not url.startswith(self._base_url + "/"):
            raise ValueError("DeyeCloud request is outside the configured API")
        kwargs["allow_redirects"] = False
        for attempt in range(2):
            token = await self.async_get_token()
            headers = dict(kwargs.get("headers") or {})
            headers["Authorization"] = f"Bearer {token}"
            kwargs["headers"] = headers
            await self._async_wait_for_request()
            async with self._session.request(method, url, **kwargs) as response:
                if response.status in (401, 403) and attempt == 0:
                    # Another task may already have replaced this token.
                    if self._token == token:
                        self._token = None
                    continue
                if response.status in (401, 403):
                    raise DeyeCloudAuthError("DeyeCloud credentials were rejected")
                if response.status == 429:
                    try:
                        delay = max(1, min(float(response.headers.get("Retry-After", 60)), 300))
                    except (TypeError, ValueError):
                        delay = 60
                    self._shared["next_request"] = max(
                        self._shared.get("next_request", 0), monotonic() + delay
                    )
                if 300 <= response.status < 400:
                    raise ValueError("DeyeCloud API redirects are not supported")
                yield response
                return

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)


def get_api_session(hass, entry):
    """Return the entry's wrapper without owning or closing HA's HTTP session."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    entry_data = hass.data.setdefault("deyecloud", {}).setdefault(entry.entry_id, {})
    if "api_session" not in entry_data:
        gate_key = (entry.data["base_url"], entry.data["app_id"])
        shared = hass.data["deyecloud"].setdefault("request_gates", {}).setdefault(gate_key, {})
        entry_data["api_session"] = DeyeCloudSession(
            async_get_clientsession(hass), entry.data, shared
        )
    return entry_data["api_session"]
