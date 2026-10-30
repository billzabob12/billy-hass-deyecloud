"""Shared, paginated station and inverter discovery."""

import logging
from datetime import timedelta

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeyeCloudAuthError, async_post_json
from .auth import get_api_session

_LOGGER = logging.getLogger(__name__)


async def async_list_all(session, base_url, path, key, **filters):
    """Read every page, rejecting malformed or repeating pages."""
    items = []
    seen = set()
    for page in range(1, 10001):
        response = await async_post_json(
            session,
            f"{base_url}{path}",
            payload={**filters, "page": page, "size": 100},
        )
        if response.get("success") is not True:
            raise UpdateFailed(f"DeyeCloud rejected discovery at {path}")
        batch = response.get(key)
        if not isinstance(batch, list):
            raise UpdateFailed(f"DeyeCloud returned invalid discovery at {path}")
        signature = repr(batch)
        if batch and signature in seen:
            raise UpdateFailed(f"DeyeCloud repeated a discovery page at {path}")
        seen.add(signature)
        items.extend(batch)
        total = response.get("total", response.get("totalCount"))
        if not batch or (total is not None and len(items) >= int(total)):
            return items
        if total is None and len(batch) < 100:
            return items
    raise UpdateFailed("DeyeCloud discovery exceeded the pagination limit")


class DeyeDiscoveryCoordinator(DataUpdateCoordinator):
    """Discover once at startup and hourly, shared by all platforms."""

    def __init__(self, hass, entry):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Deye discovery",
            update_interval=timedelta(hours=1),
        )
        self.entry = entry

    async def _async_update_data(self):
        session = get_api_session(self.hass, self.entry)
        base = self.entry.data["base_url"]
        try:
            stations = await async_list_all(session, base, "/station/list", "stationList")
            if not stations:
                raise UpdateFailed("No accessible DeyeCloud stations")
            devices = {}
            for station in stations:
                station_id = station.get("id") or station.get("stationId")
                if station_id is None:
                    continue
                batch = await async_list_all(
                    session,
                    base,
                    "/station/device",
                    "deviceListItems",
                    stationIds=[station_id],
                )
                devices[str(station_id)] = [
                    device
                    for device in batch
                    if device.get("deviceType") == "INVERTER" and device.get("deviceSn")
                ]
            return {"stations": stations, "devices": devices}
        except DeyeCloudAuthError as exc:
            raise ConfigEntryAuthFailed("DeyeCloud credentials were rejected") from exc
        except ConfigEntryAuthFailed:
            raise
        except Exception as exc:
            raise UpdateFailed(f"DeyeCloud discovery failed: {exc}") from exc


async def async_watch_inverters(hass, entry, async_add_entities, factory):
    """Add controls for existing and subsequently discovered inverters."""
    coordinator = hass.data["deyecloud"][entry.entry_id]["discovery"]
    known = set()

    async def discover():
        for devices in (coordinator.data or {}).get("devices", {}).values():
            for device in devices:
                sn = str(device["deviceSn"])
                if sn not in known:
                    entities = await factory(sn)
                    known.add(sn)
                    async_add_entities(entities)

    # Schedule a single task at a time; remove the listener and cancel pending
    # discovery work on unload, before it can add entities to an unloaded entry.
    task = None

    def updated():
        nonlocal task
        if task is None or task.done():
            task = hass.async_create_task(discover())

    def cancel():
        if task is not None:
            task.cancel()

    entry.async_on_unload(coordinator.async_add_listener(updated))
    entry.async_on_unload(cancel)
    await discover()
