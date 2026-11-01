"""DeyeCloud API helpers."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

_CONTROL_MAX_ATTEMPTS = 3
_CONTROL_RETRY_DELAY_SECONDS = 2


class DeyeCloudApiError(Exception):
    """Base error for DeyeCloud API calls."""


class DeyeCloudAuthError(DeyeCloudApiError):
    """Credentials are no longer accepted."""


class DeyeCloudOrderError(DeyeCloudApiError):
    """Raised when a DeyeCloud command order fails."""


class DeyeCloudOrderTimeout(DeyeCloudApiError):
    """Raised when a DeyeCloud command order does not complete in time."""


def _sha256(password: str) -> str:
    """Hash DeyeCloud password."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest().lower()


def _build_login_payload(login: str) -> dict[str, str]:
    """Build DeyeCloud login payload using either email or username."""
    login = login.strip()

    if "@" in login:
        return {"email": login}

    return {"username": login}


async def async_get_token(
    session: aiohttp.ClientSession,
    username,
    password,
    app_id,
    app_secret,
    base_url,
    company_id=None,
):
    """Get DeyeCloud access token."""
    if hasattr(session, "async_get_token"):
        return await session.async_get_token()

    from .auth import normalize_base_url

    base_url = normalize_base_url(base_url)
    url = f"{base_url}/account/token?appId={app_id}"

    payload = {
        "appSecret": app_secret,
        **_build_login_payload(username),
        "password": _sha256(password),
    }

    if company_id:
        payload["companyId"] = str(company_id).strip()

    async with session.post(url, json=payload, timeout=10, allow_redirects=False) as resp:
        if resp.status in (401, 403):
            raise DeyeCloudAuthError("DeyeCloud credentials were rejected")
        resp.raise_for_status()
        if 300 <= resp.status < 400:
            raise DeyeCloudApiError("Token endpoint redirects are not supported")
        data = await resp.json()

    if not data.get("success"):
        raise DeyeCloudAuthError("DeyeCloud rejected the token request")

    token = data.get("accessToken")
    if not isinstance(token, str) or not token:
        raise DeyeCloudApiError("DeyeCloud returned no access token")
    return token


async def async_post_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    token=None,
    payload: dict[str, Any] | None = None,
    timeout: int = 10,
):
    """POST JSON to DeyeCloud."""
    headers = {"Content-Type": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with session.post(url, json=payload or {}, headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        return await resp.json()


async def async_get_battery_config(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
):
    """Read current DeyeCloud battery-related parameter values."""
    url = f"{base_url}/config/battery"

    payload = {
        "deviceSn": device_sn,
    }

    data = await async_post_json(
        session,
        url,
        token=token,
        payload=payload,
        timeout=10,
    )

    if not data.get("success", True):
        raise DeyeCloudApiError(f"Battery config request failed: {data.get('msg') or data}")

    return data


def _extract_order_id(data: dict[str, Any]):
    """Extract orderId from a DeyeCloud response."""
    if not isinstance(data, dict):
        return None

    nested = data.get("data") if isinstance(data.get("data"), dict) else {}

    return (
        data.get("orderId")
        or data.get("orderID")
        or data.get("order_id")
        or nested.get("orderId")
        or nested.get("orderID")
        or nested.get("order_id")
    )


def _extract_nested_data(data: dict[str, Any]) -> dict[str, Any]:
    """Return nested response data if available."""
    nested = data.get("data")

    if isinstance(nested, dict):
        return nested

    return {}


def _extract_order_status(data: dict[str, Any]):
    """Extract a possible DeyeCloud order status field."""
    if not isinstance(data, dict):
        return None

    nested = _extract_nested_data(data)

    for source in (data, nested):
        for key in (
            "status",
            "orderStatus",
            "executeStatus",
            "executionStatus",
            "commandStatus",
            "result",
            "state",
        ):
            if key in source:
                return source[key]

    return None


def _classify_order_result(data: dict[str, Any]) -> str:
    """Classify a DeyeCloud command result."""
    if not isinstance(data, dict):
        return "pending"

    if data.get("success") is False:
        return "pending"

    status = _extract_order_status(data)
    status_text = str(status).strip().upper() if status is not None else ""

    # API success means the query succeeded, not that the inverter executed it.
    if status_text in {"0", "100", "300"}:
        return "pending"
    if status_text == "666":
        return "success"
    if status_text in {"400", "500"}:
        return "failed"
    # Keep compatibility with explicit textual terminal results.
    if status_text in {"SUCCESS", "SUCCESSFUL", "DONE", "COMPLETED", "EXECUTED"}:
        return "success"
    if status_text in {"FAILED", "FAILURE", "ERROR", "TIMEOUT", "REJECTED"}:
        return "failed"

    return "pending"


async def async_get_order_result(
    session: aiohttp.ClientSession,
    token,
    base_url,
    order_id,
):
    """Get DeyeCloud command execution result."""
    url = f"{base_url}/order/{order_id}"
    headers = {"Authorization": f"Bearer {token}"}

    async with session.get(url, headers=headers, timeout=10) as resp:
        resp.raise_for_status()
        return await resp.json()


async def async_wait_for_order_result(
    session: aiohttp.ClientSession,
    token,
    base_url,
    order_id,
    *,
    timeout_seconds: int = 90,
    poll_interval_seconds: int = 3,
):
    """Poll DeyeCloud until command order succeeds, fails, or times out."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_result = None

    while asyncio.get_running_loop().time() < deadline:
        result = await async_get_order_result(
            session,
            token,
            base_url,
            order_id,
        )

        last_result = result
        classification = _classify_order_result(result)

        _LOGGER.debug(
            "DeyeCloud order %s poll result: %s classified as %s",
            order_id,
            result,
            classification,
        )

        if classification == "success":
            return result

        if classification == "failed":
            raise DeyeCloudOrderError(f"DeyeCloud order {order_id} failed: {result}")

        await asyncio.sleep(poll_interval_seconds)

    raise DeyeCloudOrderTimeout(
        f"DeyeCloud order {order_id} did not complete within "
        f"{timeout_seconds}s. Last result: {last_result}"
    )


async def _async_post_control(
    session: aiohttp.ClientSession,
    token,
    base_url,
    path: str,
    payload: dict[str, Any],
    *,
    wait_for_result: bool = True,
    timeout_seconds: int = 90,
    poll_interval_seconds: int = 3,
):
    """POST a DeyeCloud control command and optionally poll its orderId."""
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Retry submission only: once an order is accepted, never resubmit it
    # because polling fails. These commands set explicit values, not toggles.
    for attempt in range(1, _CONTROL_MAX_ATTEMPTS + 1):
        try:
            async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
                resp.raise_for_status()
                data = await resp.json()
            break
        except aiohttp.ClientResponseError as exc:
            if exc.status != 500 or attempt == _CONTROL_MAX_ATTEMPTS:
                raise
            delay = _CONTROL_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
            _LOGGER.warning(
                "DeyeCloud control endpoint %s returned HTTP 500 "
                "(attempt %s/%s); retrying in %s seconds",
                path,
                attempt,
                _CONTROL_MAX_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

    if not data.get("success", True):
        raise DeyeCloudOrderError(f"DeyeCloud control request failed: {data.get('msg') or data}")

    if not wait_for_result:
        return data

    order_id = _extract_order_id(data)

    if not order_id:
        raise DeyeCloudOrderError(
            f"DeyeCloud command was accepted but no orderId was returned: {data}"
        )

    order_result = await async_wait_for_order_result(
        session,
        token,
        base_url,
        order_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )

    data["order_result"] = order_result
    return data


async def async_post_control(session, token, base_url, path, payload, **kwargs):
    """Serialize each device's orders through confirmation, including reads.

    Retain unresolved orders after timeout/network failure. Before submitting a
    new command, finish polling that order; never blindly replay it.
    """
    if not hasattr(session, "command_lock"):
        return await _async_post_control(session, token, base_url, path, payload, **kwargs)
    sn = str(payload["deviceSn"])
    async with session.command_lock(sn):
        if path == "/strategy/dynamicControl/read":
            # Reads cannot change inverter settings. Keep write uncertainty intact,
            # but allow fresh observations and do not poison writes if a read fails.
            return await _async_post_control(session, token, base_url, path, payload, **kwargs)
        pending = session.pending_orders
        if sn in pending:
            order_id = pending[sn]
            if order_id is None:
                raise DeyeCloudOrderError(
                    "Previous submission has an uncertain result; verify the inverter "
                    "and restart Home Assistant before sending another command"
                )
            try:
                await async_wait_for_order_result(session, token, base_url, order_id)
            except DeyeCloudOrderError:
                pending.pop(sn, None)
            else:
                pending.pop(sn, None)
        try:
            result = await _async_post_control(
                session,
                token,
                base_url,
                path,
                payload,
                **{**kwargs, "wait_for_result": False},
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status >= 500:
                pending[sn] = None
            raise
        except (
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            ValueError,
            asyncio.CancelledError,
        ):
            pending[sn] = None
            raise
        order_id = _extract_order_id(result)
        pending[sn] = order_id
        if not kwargs.get("wait_for_result", True):
            return result
        if order_id is None:
            raise DeyeCloudOrderError("DeyeCloud accepted the command without an orderId")
        try:
            result["order_result"] = await async_wait_for_order_result(
                session,
                token,
                base_url,
                order_id,
                timeout_seconds=kwargs.get("timeout_seconds", 90),
                poll_interval_seconds=kwargs.get("poll_interval_seconds", 3),
            )
        except DeyeCloudOrderError:
            pending.pop(sn, None)
            raise
        else:
            pending.pop(sn, None)
            return result


async def async_control_solar_sell(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
    is_enable,
    *,
    wait_for_result: bool = True,
):
    """Send Solar Sell control command."""
    return await async_post_control(
        session,
        token,
        base_url,
        "/order/sys/solarSell/control",
        {
            "action": "on" if is_enable else "off",
            "deviceSn": device_sn,
        },
        wait_for_result=wait_for_result,
    )


async def async_control_grid_charge(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
    is_enable: bool,
    *,
    wait_for_result: bool = True,
):
    """Enable or disable Deye grid charge mode."""
    return await async_post_control(
        session,
        token,
        base_url,
        "/order/battery/modeControl",
        {
            "deviceSn": device_sn,
            "batteryModeType": "GRID_CHARGE",
            "action": "on" if is_enable else "off",
        },
        wait_for_result=wait_for_result,
    )


async def async_update_battery_parameter(
    session: aiohttp.ClientSession,
    token,
    base_url,
    device_sn,
    parameter_type: str,
    value: float,
    *,
    wait_for_result: bool = True,
):
    """Set a battery parameter using Deye's documented 'paramterType' key.

    Do not resubmit a command after a timeout or an uncertain order result:
    the original command may already have reached the inverter.
    """
    return await async_post_control(
        session,
        token,
        base_url,
        "/order/battery/parameter/update",
        {"deviceSn": device_sn, "paramterType": parameter_type, "value": value},
        wait_for_result=wait_for_result,
    )
