"""DeyeCloud sensor entities."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta

import aiohttp
from dateutil.relativedelta import relativedelta
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import DeyeCloudAuthError, async_get_token
from .auth import get_api_session
from .battery_parameters import (
    BATTERY_PARAMETER_DESCRIPTIONS,
    extract_battery_parameter,
)
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
from .settings import async_get_settings_coordinator

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)
HISTORY_REFRESH_INTERVAL = timedelta(hours=6)
DEFAULT_HISTORY_START_MONTH = "2024-01"

_RELATIVE_DAY_OFFSETS = {
    "today": 0,
    "yesterday": 1,
    "day_before": 2,
}

_DAILY_LABELS = {
    "day_before": "Day Before Yesterday",
    "yesterday": "Yesterday",
    "today": "Today",
}


def _parse_api_date(value) -> date | None:
    """Parse a DeyeCloud date-like value into a date if possible."""
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    if not text:
        return None

    text = text.replace("/", "-")

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _select_daily_record(
    daily_items: list[dict],
    day: str,
    *,
    allow_undated_fallback: bool,
) -> dict | None:
    """Select only the record that actually belongs to the requested day."""
    target = datetime.strptime(day, "%Y-%m-%d").date()
    has_date_field = False

    for item in daily_items:
        item_date = _parse_api_date(
            item.get("date")
            or item.get("time")
            or item.get("timestamp")
            or item.get("collectionTime")
        )

        if item_date is None:
            continue

        has_date_field = True

        if item_date == target:
            return item

    if allow_undated_fallback and not has_date_field and len(daily_items) == 1:
        return daily_items[0]

    return None


def _resolve_daily_date_key(date_key: str) -> str:
    """Convert relative day key to YYYY-MM-DD using HA timezone."""
    if date_key in _RELATIVE_DAY_OFFSETS:
        d = dt_util.now().date() - timedelta(days=_RELATIVE_DAY_OFFSETS[date_key])
        return d.isoformat()

    return date_key


def _as_list(value):
    """Return value if it is a list, otherwise return an empty list."""
    return value if isinstance(value, list) else []


def _as_float_or_original(value):
    """Return numeric values as float, otherwise return original value."""
    if value is None:
        return None

    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _normalize_unit(unit: str | None) -> str | None:
    """Normalize common API units for Home Assistant."""
    if unit == "C":
        return "°C"

    return unit


def _validate_history_start_month(value: str | None) -> str:
    """Validate YYYY-MM start month."""
    if not value:
        return "2024-01"

    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError:
        _LOGGER.warning("Invalid start month %s, falling back to 2024-01", value)
        return "2024-01"

    return value


async def _post_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers=None,
    payload=None,
    timeout=10,
):
    """POST JSON with one retry for temporary network/server errors."""
    last_exc = None

    for attempt in range(2):
        try:
            async with session.post(
                url,
                headers=headers,
                json=payload or {},
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc

            if isinstance(exc, aiohttp.ClientResponseError) and exc.status not in (
                500,
                502,
                503,
                504,
            ):
                raise

            if attempt == 1:
                break

            await asyncio.sleep(1)

    raise last_exc


async def _async_history(session, token, station_id, base_url, start_month):
    """Fetch monthly history from the entry start month to current month."""
    url = f"{base_url}/station/history"
    headers = {"Authorization": f"Bearer {token}"}

    items: list[dict] = []

    start_dt = datetime.strptime(start_month, "%Y-%m")
    start: date = start_dt.date().replace(day=1)
    end: date = dt_util.now().date().replace(day=1)

    _LOGGER.debug(
        "Fetching monthly history for station_id %s from %s to %s",
        station_id,
        start.strftime("%Y-%m"),
        end.strftime("%Y-%m"),
    )

    while start <= end:
        range_start: date = start
        range_end: date = min(range_start + relativedelta(months=11), end)

        payload = {
            "stationId": station_id,
            "granularity": 3,
            "startAt": range_start.strftime("%Y-%m"),
            "endAt": range_end.strftime("%Y-%m"),
        }

        data = await _post_json(session, url, headers=headers, payload=payload, timeout=10)

        if not data.get("success"):
            _LOGGER.error(
                "Monthly history request failed for station_id %s: %s",
                station_id,
                data.get("msg"),
            )
            raise UpdateFailed(f"History request failed: {data.get('msg')}")

        items.extend(_as_list(data.get("stationDataItems")))

        start = range_end + relativedelta(months=1)

    _LOGGER.debug("Received %d monthly records for station_id %s", len(items), station_id)

    return items


async def _async_daily_history(session, token, station_id, base_url, start_date, end_date):
    """Fetch daily history from DeyeCloud."""
    url = f"{base_url}/station/history"
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "stationId": station_id,
        "granularity": 2,
        "startAt": start_date,
        "endAt": end_date,
    }

    _LOGGER.debug(
        "Fetching daily data for station_id %s from %s to %s",
        station_id,
        start_date,
        end_date,
    )

    data = await _post_json(session, url, headers=headers, payload=payload, timeout=10)

    if not data.get("success"):
        _LOGGER.error(
            "Daily history request failed for station_id %s: %s",
            station_id,
            data.get("msg"),
        )
        raise UpdateFailed(f"Daily history request failed: {data.get('msg')}")

    items = _as_list(data.get("stationDataItems"))

    _LOGGER.debug("Received %d daily records for station_id %s", len(items), station_id)

    return items


async def _async_get_device_status(session, token, base_url, device_list):
    """Fetch latest inverter device status from DeyeCloud."""
    if not device_list:
        return []

    url = f"{base_url}/device/latest"

    _LOGGER.debug("Fetching device status from API: %s with devices: %s", url, device_list)

    headers = {"Authorization": f"Bearer {token}"}
    payload = {"deviceList": device_list}

    data = await _post_json(session, url, headers=headers, payload=payload, timeout=10)

    if not data.get("success"):
        _LOGGER.error("Device status request failed: %s", data.get("msg"))
        raise UpdateFailed(f"Device status request failed: {data.get('msg')}")

    _LOGGER.debug("Received device status: %s", data)

    return _as_list(data.get("deviceDataList"))


class DeyeCloudCoordinator(DataUpdateCoordinator):
    """Coordinator for Deye Cloud data updates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Deye Cloud",
            config_entry=entry,
            update_interval=SCAN_INTERVAL,
        )

        self.entry = entry
        self.session = get_api_session(hass, entry)
        self.token = None
        self._history_cache: dict[str, list[dict]] = {}
        self._history_last_update = {}
        self._start_month = _validate_history_start_month(entry.data.get(CONF_START_MONTH))
        self._daily_cache = {}
        self._daily_last_update = {}
        self._station_semaphore = asyncio.Semaphore(3)

    async def _async_update_data(self) -> dict:
        """Fetch data from DeyeCloud."""
        username = self.entry.data[CONF_USERNAME]
        password = self.entry.data[CONF_PASSWORD]
        app_id = self.entry.data[CONF_APP_ID]
        app_secret = self.entry.data[CONF_APP_SECRET]
        base_url = self.entry.data[CONF_BASE_URL]
        company_id = self.entry.data.get(CONF_COMPANY_ID)

        try:
            self.token = await async_get_token(
                self.session,
                username,
                password,
                app_id,
                app_secret,
                base_url,
                company_id,
            )
        except DeyeCloudAuthError as exc:
            raise ConfigEntryAuthFailed("DeyeCloud credentials were rejected") from exc
        except Exception as exc:
            raise UpdateFailed(f"Token refresh failed: {exc}") from exc

        try:
            discovery = self.hass.data[DOMAIN][self.entry.entry_id]["discovery"]
            stations = (discovery.data or {}).get("stations", [])

            if not stations:
                raise UpdateFailed("No stations found")

        except Exception as exc:
            raise UpdateFailed(f"Error fetching stations: {exc}") from exc

        station_tasks = []

        for station in stations:
            raw_station_id = station.get("id") or station.get("stationId")

            if raw_station_id:
                station_id = str(raw_station_id)
                station_tasks.append(
                    self._async_update_station_data(
                        self.session,
                        station_id,
                        base_url,
                        station,
                    )
                )

        station_data = {}

        results = await asyncio.gather(*station_tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, (DeyeCloudAuthError, ConfigEntryAuthFailed)):
                raise ConfigEntryAuthFailed("DeyeCloud credentials were rejected") from result
            if isinstance(result, Exception):
                _LOGGER.error("Error updating station data: %s", result)
            elif result:
                station_id, data = result
                station_data[station_id] = data

        return station_data

    async def _get_monthly_history_cached(self, session, station_id, base_url):
        """Keep a separate refresh timestamp for each station."""
        now = dt_util.now()
        last = self._history_last_update.get(station_id)
        history = self._history_cache.get(station_id, [])
        has_current = any(
            record.get("year") == now.year and record.get("month") == now.month
            for record in history
        )
        interval = HISTORY_REFRESH_INTERVAL if has_current else timedelta(minutes=10)
        if last is None or now - last >= interval:
            self._history_cache[station_id] = await _async_history(
                session,
                self.token,
                station_id,
                base_url,
                self._start_month,
            )
            self._history_last_update[station_id] = now
        return self._history_cache.get(station_id, [])

    async def _async_update_station_data(self, session, station_id, base_url, station_info):
        async with self._station_semaphore:
            return await self._async_fetch_station_data(session, station_id, base_url, station_info)

    async def _async_fetch_station_data(self, session, station_id, base_url, station_info):
        """Return unknown readings on failure, never manufactured zero energy."""
        now = dt_util.now()
        today = now.date()
        data = {
            "info": station_info,
            "history": [],
            "daily": {},
            "devices": {},
            "battery_config": {},
            "history_read_at": None,
            "daily_read_at": None,
        }
        try:
            data["history"] = await self._get_monthly_history_cached(session, station_id, base_url)
        except DeyeCloudAuthError:
            raise
        except Exception as exc:
            _LOGGER.warning("Monthly history unavailable for station %s: %s", station_id, exc)
            data["history"] = self._history_cache.get(station_id, [])
        last = self._history_last_update.get(station_id)
        data["history_read_at"] = last.isoformat() if last else None

        # Completed days are refreshed daily; today's total remains live.
        last_daily = self._daily_last_update.get(station_id)
        refresh_completed = last_daily is None or now - last_daily >= timedelta(hours=24)
        cached = self._daily_cache.get(station_id, {})
        wanted = [(today - timedelta(days=offset)).isoformat() for offset in (2, 1, 0)]
        if any(day not in cached for day in wanted[:-1]):
            refresh_completed = True
        start = wanted[0] if refresh_completed else wanted[-1]
        data["daily"] = {day: cached[day] for day in wanted[:-1] if day in cached}
        try:
            items = await _async_daily_history(
                session, self.token, station_id, base_url, start, wanted[-1]
            )
            for day in wanted if refresh_completed else wanted[-1:]:
                record = _select_daily_record(items, day, allow_undated_fallback=False)
                if record is not None:
                    data["daily"][day] = record
            self._daily_cache[station_id] = dict(data["daily"])
            if refresh_completed:
                self._daily_last_update[station_id] = now
            data["daily_read_at"] = now.isoformat()
        except DeyeCloudAuthError:
            raise
        except Exception as exc:
            _LOGGER.warning("Daily readings unavailable for station %s: %s", station_id, exc)

        discovery = self.hass.data[DOMAIN][self.entry.entry_id]["discovery"]
        devices = (discovery.data or {}).get("devices", {}).get(station_id, [])
        serials = [str(device["deviceSn"]) for device in devices]
        # Preserve discovery metadata so battery sensors exist before telemetry.
        data["devices"] = {str(device["deviceSn"]): dict(device) for device in devices}
        try:
            for offset in range(0, len(serials), 100):
                status = await _async_get_device_status(
                    session, self.token, base_url, serials[offset : offset + 100]
                )
                for device in status:
                    if device.get("deviceSn"):
                        data["devices"][str(device["deviceSn"])] = device
        except DeyeCloudAuthError:
            raise
        except Exception as exc:
            _LOGGER.warning("Device readings unavailable for station %s: %s", station_id, exc)
        for sn in serials:
            settings = await async_get_settings_coordinator(self.hass, self.entry, sn)
            if settings.last_update_success:
                data["battery_config"][sn] = settings.battery_config
        return station_id, data


class DeyeCloudSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Deye Cloud Sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DeyeCloudCoordinator,
        sensor_type: str,
        name: str,
        unique_id: str,
        unit: str | None = None,
        device_class: str | None = None,
        state_class: str | None = None,
        extra_attributes: dict | None = None,
        station_id: str | None = None,
        date_key: str | None = None,
        metric_key: str | None = None,
        device_sn: str | None = None,
        device_key: str | None = None,
        parameter_types: tuple[str, ...] | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._sensor_type = sensor_type
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_native_unit_of_measurement = _normalize_unit(unit)

        if device_class:
            self._attr_device_class = device_class

        if state_class:
            self._attr_state_class = state_class

        self._extra_attributes = extra_attributes or {}
        self._station_id = str(station_id) if station_id is not None else None
        self._date_key = date_key
        self._metric_key = metric_key
        self._device_sn = str(device_sn) if device_sn is not None else None
        self._device_key = device_key
        self._parameter_types = parameter_types

    @property
    def native_value(self):
        """Return the sensor value."""
        if not self.coordinator.data or not self._station_id:
            return None

        station_data = self.coordinator.data.get(self._station_id)

        if not station_data:
            return None

        try:
            if self._sensor_type == "monthly_raw":
                year, month = map(int, self._date_key.split("_"))

                for record in station_data.get("history", []):
                    if record.get("year") == year and record.get("month") == month:
                        return _as_float_or_original(record.get("generationValue"))

            elif self._sensor_type == "monthly_metric":
                if self._date_key == "current":
                    target = dt_util.now()
                else:
                    target = dt_util.now() - relativedelta(months=1)

                for record in station_data.get("history", []):
                    if record.get("year") == target.year and record.get("month") == target.month:
                        return _as_float_or_original(record.get(self._metric_key))

            elif self._sensor_type == "daily":
                date_str = _resolve_daily_date_key(self._date_key)
                daily_data = station_data.get("daily", {}).get(date_str, {})

                return _as_float_or_original(daily_data.get(self._metric_key))

            elif self._sensor_type == "device":
                device_data = station_data.get("devices", {}).get(self._device_sn, {})

                for data_item in device_data.get("dataList") or []:
                    if data_item.get("key") == self._device_key:
                        return _as_float_or_original(data_item.get("value"))

            elif self._sensor_type == "battery_parameter":
                battery_config = station_data.get("battery_config", {}).get(
                    self._device_sn,
                    {},
                )

                return extract_battery_parameter(
                    battery_config,
                    self._parameter_types or (),
                )

        except (KeyError, ValueError, TypeError) as exc:
            _LOGGER.error("Error extracting value for %s: %s", self.unique_id, exc)

        return None

    @property
    def available(self):
        return super().available and self.native_value is not None

    @property
    def device_info(self):
        """Return device information."""
        if self._device_sn:
            return {
                "identifiers": {(DOMAIN, self._device_sn)},
                "name": f"Deye Inverter {self._device_sn}",
                "manufacturer": "Deye",
                "model": "Inverter",
            }

        if self._station_id:
            return {
                "identifiers": {(DOMAIN, f"station_{self._station_id}")},
                "name": f"Deye Station {self._station_id}",
                "manufacturer": "Deye",
                "model": "Station",
            }

        return None

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        attrs = self._extra_attributes.copy()
        station = (self.coordinator.data or {}).get(self._station_id, {})
        if self._sensor_type == "monthly_raw":
            year, month = map(int, self._date_key.split("_"))
            attrs.update(
                next(
                    (
                        record
                        for record in station.get("history", [])
                        if record.get("year") == year and record.get("month") == month
                    ),
                    {},
                )
            )
        if self._sensor_type.startswith("monthly"):
            attrs["data_read_at"] = station.get("history_read_at")
        elif self._sensor_type == "daily":
            attrs["data_read_at"] = station.get("daily_read_at")
        if self._sensor_type == "device":
            device = station.get("devices", {}).get(self._device_sn, {})
            attrs.update(
                device_type=device.get("deviceType"),
                device_state=device.get("deviceState"),
                collection_time=device.get("collectionTime"),
            )

        if self._station_id:
            attrs["station_id"] = self._station_id

        if self._date_key:
            if self._sensor_type == "monthly_raw":
                attrs["year"] = int(self._date_key.split("_")[0])
                attrs["month"] = int(self._date_key.split("_")[1])

            elif self._sensor_type == "monthly_metric":
                if self._date_key == "current":
                    target = dt_util.now()
                else:
                    target = dt_util.now() - relativedelta(months=1)

                attrs["year"] = target.year
                attrs["month"] = target.month
                attrs["metric_key"] = self._metric_key

            elif self._sensor_type == "daily":
                attrs["relative_day"] = self._date_key
                attrs["date"] = _resolve_daily_date_key(self._date_key)

        if self._device_sn:
            attrs["device_sn"] = self._device_sn

        return attrs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up Deye Cloud sensors from a config entry."""
    _LOGGER.info("Setting up DeyeCloud integration")

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    known = set()

    monthly_metrics = [
        ("generationValue", "Solar Generation"),
        ("consumptionValue", "Monthly Consumption"),
        ("gridValue", "Monthly Grid Export"),
        ("purchaseValue", "Monthly Grid Import"),
        ("chargeValue", "Monthly Battery Charge"),
        ("dischargeValue", "Monthly Battery Discharge"),
    ]

    daily_metrics = [
        ("generationValue", "Solar Generation"),
        ("consumptionValue", "Daily Consumption"),
        ("gridValue", "Daily Grid Export"),
        ("purchaseValue", "Daily Grid Import"),
        ("chargeValue", "Daily Battery Charge"),
        ("dischargeValue", "Daily Battery Discharge"),
    ]

    @callback
    def discover_sensors():
        entities = []
        for station_id, station_data in coordinator.data.items():
            station_id = str(station_id)

            for record in station_data.get("history", []):
                y = record.get("year")
                m = record.get("month")

                if not y or not m:
                    continue

                month_name = datetime(year=y, month=m, day=1).strftime("%b %Y")

                name = f"Deye {station_id} {month_name}"
                uid = f"{station_id}_raw_{y}_{m:02d}"

                entities.append(
                    DeyeCloudSensor(
                        coordinator=coordinator,
                        sensor_type="monthly_raw",
                        name=name,
                        unique_id=uid,
                        unit="kWh",
                        device_class="energy",
                        state_class="total",
                        station_id=station_id,
                        date_key=f"{y}_{m}",
                        extra_attributes=record,
                    )
                )

            for metric_key, metric_name in monthly_metrics:
                name = f"{metric_name} {station_id}"
                uid = f"{station_id}_{metric_key}_current_month"

                entities.append(
                    DeyeCloudSensor(
                        coordinator=coordinator,
                        sensor_type="monthly_metric",
                        name=name,
                        unique_id=uid,
                        unit="kWh",
                        device_class="energy",
                        state_class="total_increasing",
                        station_id=station_id,
                        date_key="current",
                        metric_key=metric_key,
                        extra_attributes={"metric": metric_name},
                    )
                )

                name = f"{metric_name} Last Month {station_id}"
                uid = f"{station_id}_{metric_key}_last_month"

                entities.append(
                    DeyeCloudSensor(
                        coordinator=coordinator,
                        sensor_type="monthly_metric",
                        name=name,
                        unique_id=uid,
                        unit="kWh",
                        device_class="energy",
                        state_class="total",
                        station_id=station_id,
                        date_key="last",
                        metric_key=metric_key,
                        extra_attributes={"metric": metric_name},
                    )
                )

            for rel_key, label in _DAILY_LABELS.items():
                for metric_key, metric_name in daily_metrics:
                    name = f"{metric_name} {label} {station_id}"
                    uid = f"{station_id}_{metric_key}_{rel_key}"

                    entities.append(
                        DeyeCloudSensor(
                            coordinator=coordinator,
                            sensor_type="daily",
                            name=name,
                            unique_id=uid,
                            unit="kWh",
                            device_class="energy",
                            state_class="total_increasing" if rel_key == "today" else "total",
                            station_id=station_id,
                            date_key=rel_key,
                            metric_key=metric_key,
                            extra_attributes={"relative_day": rel_key},
                        )
                    )

            for device_sn, device_data in station_data.get("devices", {}).items():
                device_sn = str(device_sn)

                for data_item in device_data.get("dataList") or []:
                    key = data_item.get("key")

                    if not key:
                        continue

                    name = f"{key} {device_sn}"
                    uid = f"device_{device_sn}_{key}"
                    unit = _normalize_unit(data_item.get("unit", ""))

                    unit_device_class = None
                    unit_state_class = None

                    if unit == "kWh":
                        unit_device_class = "energy"
                        unit_state_class = "total"

                    elif unit == "W":
                        unit_device_class = "power"
                        unit_state_class = "measurement"

                    elif unit == "V":
                        unit_device_class = "voltage"
                        unit_state_class = "measurement"

                    elif unit == "A":
                        unit_device_class = "current"
                        unit_state_class = "measurement"

                    elif unit == "%":
                        unit_device_class = (
                            "battery" if str(key).lower() in {"soc", "batterysoc"} else None
                        )
                        unit_state_class = "measurement"

                    elif unit == "°C":
                        unit_device_class = "temperature"
                        unit_state_class = "measurement"

                    elif unit == "Hz":
                        unit_device_class = "frequency"
                        unit_state_class = "measurement"

                    entities.append(
                        DeyeCloudSensor(
                            coordinator=coordinator,
                            sensor_type="device",
                            name=name,
                            unique_id=uid,
                            unit=unit,
                            device_class=unit_device_class,
                            state_class=unit_state_class,
                            station_id=station_id,
                            device_sn=device_sn,
                            device_key=key,
                            extra_attributes={
                                "device_type": device_data.get("deviceType"),
                                "device_state": device_data.get("deviceState"),
                                "collection_time": device_data.get("collectionTime"),
                            },
                        )
                    )

                for description in BATTERY_PARAMETER_DESCRIPTIONS:
                    entities.append(
                        DeyeCloudSensor(
                            coordinator=coordinator,
                            sensor_type="battery_parameter",
                            name=f"{description.name} {device_sn}",
                            unique_id=f"device_{device_sn}_{description.key}_sensor",
                            unit=description.unit,
                            device_class="current",
                            state_class="measurement",
                            station_id=station_id,
                            device_sn=device_sn,
                            parameter_types=description.parameter_types,
                            extra_attributes={
                                "source": "config/battery",
                                "parameter_types": list(description.parameter_types),
                            },
                        )
                    )

        fresh = [entity for entity in entities if entity.unique_id not in known]
        known.update(entity.unique_id for entity in fresh)
        if fresh:
            async_add_entities(fresh)

    entry.async_on_unload(coordinator.async_add_listener(discover_sensors))
    discover_sensors()
