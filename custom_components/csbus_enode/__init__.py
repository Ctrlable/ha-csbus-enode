"""
Converging Systems e-Node CS-Bus integration for Home Assistant.

Sets up the Telnet connection, runs device discovery, and coordinates
state updates for lights and covers.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_COVER_ENTITIES,
    DATA_DEVICES,
    DATA_LIGHT_ENTITIES,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DEVICE_CLASS_LIGHT,
    DEVICE_CLASS_MOTOR,
    DOMAIN,
    PLATFORM_COVER,
    PLATFORM_LIGHT,
    SERVICE_RECALL_PRESET,
    SERVICE_RESUME_CIRCADIAN,
    SERVICE_SET_CIRCADIAN,
    SERVICE_SET_DISSOLVE,
    SERVICE_STORE_PRESET,
)
from .enode_client import ENodeClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT, Platform.COVER, Platform.SENSOR]

_ALL_SERVICES = [
    SERVICE_RECALL_PRESET,
    SERVICE_STORE_PRESET,
    SERVICE_SET_CIRCADIAN,
    SERVICE_RESUME_CIRCADIAN,
    SERVICE_SET_DISSOLVE,
]

# ---------------------------------------------------------------------------
# Service schemas
# ---------------------------------------------------------------------------

_SCHEMA_RECALL_PRESET = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required("preset"): vol.All(vol.Coerce(int), vol.Range(min=0, max=24)),
        vol.Optional("transition"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    },
    extra=vol.ALLOW_EXTRA,
)

_SCHEMA_STORE_PRESET = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required("preset"): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
    },
    extra=vol.ALLOW_EXTRA,
)

_SCHEMA_SET_CIRCADIAN = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required("level"): vol.All(vol.Coerce(int), vol.Range(min=0, max=240)),
        vol.Optional("transition"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    },
    extra=vol.ALLOW_EXTRA,
)

_SCHEMA_RESUME_CIRCADIAN = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional("max_level", default=240): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=240)
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

_SCHEMA_SET_DISSOLVE = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required("dissolve_index"): vol.All(vol.Coerce(int), vol.Range(min=0, max=4)),
        vol.Required("seconds"): vol.All(vol.Coerce(float), vol.Range(min=0)),
    },
    extra=vol.ALLOW_EXTRA,
)


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the e-Node integration from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    username = entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)
    password = entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD)
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    client = ENodeClient(host=host, port=port, username=username, password=password)

    if not await client.async_connect():
        _LOGGER.error("Failed to connect to e-Node at %s", host)
        return False

    # Enable push notifications so the e-Node reports state changes immediately
    await client.async_send_command("0.0.0", "LED", "NOTIFY=VALUE")
    await client.async_send_command("0.0.0", "MOTOR", "NOTIFY=ON")

    # Run device discovery
    raw_devices = await client.async_discover()
    _LOGGER.debug("e-Node discovery returned %d raw devices", len(raw_devices))

    parsed_devices = _parse_devices(raw_devices)
    _LOGGER.info(
        "e-Node discovered %d devices (%d lights, %d covers)",
        len(parsed_devices),
        sum(1 for d in parsed_devices if d["platform"] == PLATFORM_LIGHT),
        sum(1 for d in parsed_devices if d["platform"] == PLATFORM_COVER),
    )

    coordinator = ENodeCoordinator(hass, client, parsed_devices, scan_interval)
    client.add_listener(coordinator.handle_message)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
        DATA_DEVICES: parsed_devices,
        DATA_LIGHT_ENTITIES: [],
        DATA_COVER_ENTITIES: [],
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register domain services (idempotent — skipped if already registered)
    _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the integration."""
    unloaded: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data: dict[str, Any] = hass.data[DOMAIN].pop(entry.entry_id)
        client: ENodeClient = data[DATA_CLIENT]
        await client.async_disconnect()
        # Remove services only when the last entry is gone
        if not hass.data[DOMAIN]:
            for svc in _ALL_SERVICES:
                hass.services.async_remove(DOMAIN, svc)
    return unloaded


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# ---------------------------------------------------------------------------
# Service registration
# ---------------------------------------------------------------------------


def _get_entity_ids_from_call(call: ServiceCall) -> set[str]:
    raw = call.data.get(ATTR_ENTITY_ID)
    if isinstance(raw, list):
        return set(raw)
    if isinstance(raw, str):
        return {raw}
    return set()


def _async_register_services(hass: HomeAssistant) -> None:
    """Register all custom CS-Bus services (no-op if already registered)."""
    if hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET):
        return

    async def _handle_recall_preset(call: ServiceCall) -> None:
        entity_ids = _get_entity_ids_from_call(call)
        preset: int = int(call.data["preset"])
        transition: int | None = (
            int(call.data["transition"]) if "transition" in call.data else None
        )
        for entry_data in hass.data.get(DOMAIN, {}).values():
            for entity in entry_data.get(DATA_LIGHT_ENTITIES, []):
                if entity.entity_id in entity_ids:
                    await entity.async_recall_preset(preset, transition)
            for entity in entry_data.get(DATA_COVER_ENTITIES, []):
                if entity.entity_id in entity_ids:
                    await entity.async_recall_preset(preset)

    async def _handle_store_preset(call: ServiceCall) -> None:
        entity_ids = _get_entity_ids_from_call(call)
        preset: int = int(call.data["preset"])
        for entry_data in hass.data.get(DOMAIN, {}).values():
            for entity in entry_data.get(DATA_LIGHT_ENTITIES, []):
                if entity.entity_id in entity_ids:
                    await entity.async_store_preset(preset)
            for entity in entry_data.get(DATA_COVER_ENTITIES, []):
                if entity.entity_id in entity_ids:
                    await entity.async_store_preset(preset)

    async def _handle_set_circadian(call: ServiceCall) -> None:
        entity_ids = _get_entity_ids_from_call(call)
        level: int = int(call.data["level"])
        transition: int | None = (
            int(call.data["transition"]) if "transition" in call.data else None
        )
        for entry_data in hass.data.get(DOMAIN, {}).values():
            for entity in entry_data.get(DATA_LIGHT_ENTITIES, []):
                if entity.entity_id in entity_ids:
                    await entity.async_set_circadian(level, transition)

    async def _handle_resume_circadian(call: ServiceCall) -> None:
        entity_ids = _get_entity_ids_from_call(call)
        max_level: int = int(call.data.get("max_level", 240))
        for entry_data in hass.data.get(DOMAIN, {}).values():
            for entity in entry_data.get(DATA_LIGHT_ENTITIES, []):
                if entity.entity_id in entity_ids:
                    await entity.async_resume_circadian(max_level)

    async def _handle_set_dissolve(call: ServiceCall) -> None:
        entity_ids = _get_entity_ids_from_call(call)
        dissolve_index: int = int(call.data["dissolve_index"])
        seconds: float = float(call.data["seconds"])
        for entry_data in hass.data.get(DOMAIN, {}).values():
            for entity in entry_data.get(DATA_LIGHT_ENTITIES, []):
                if entity.entity_id in entity_ids:
                    await entity.async_set_dissolve(dissolve_index, seconds)

    hass.services.async_register(
        DOMAIN, SERVICE_RECALL_PRESET, _handle_recall_preset, schema=_SCHEMA_RECALL_PRESET
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STORE_PRESET, _handle_store_preset, schema=_SCHEMA_STORE_PRESET
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CIRCADIAN, _handle_set_circadian, schema=_SCHEMA_SET_CIRCADIAN
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESUME_CIRCADIAN,
        _handle_resume_circadian,
        schema=_SCHEMA_RESUME_CIRCADIAN,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_DISSOLVE, _handle_set_dissolve, schema=_SCHEMA_SET_DISSOLVE
    )


# ---------------------------------------------------------------------------
# Device parsing helpers
# ---------------------------------------------------------------------------


def _parse_devices(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw discovery dicts into normalized device descriptors."""
    devices: list[dict[str, Any]] = []
    for d in raw:
        form_str = d.get("form", "")
        form = _parse_form(form_str)
        device_class = form.get("type", "").upper()

        if device_class == DEVICE_CLASS_LIGHT:
            devices.append(
                {
                    "uid": d.get("uid", ""),
                    "alias": d.get("alias", f"CS-Bus Light {d.get('uid','')}"),
                    "address": d.get("address", "2.1.0"),
                    "platform": PLATFORM_LIGHT,
                    "device_class": device_class,
                    "color_space": form.get("color_space", "MONO"),
                    "cct_support": form.get("cct_support", False),
                    "cct_warm": d.get("cct_warm", 2700),
                    "cct_cool": d.get("cct_cool", 6500),
                    "channels": form.get("channels", 1),
                    "bus_type": form.get("bus_type", "I"),
                    "type_name": d.get("type", "ILC"),
                }
            )

        elif device_class == DEVICE_CLASS_MOTOR:
            channel_aliases: dict[str, str] = d.get("channel_aliases", {})
            channel_addresses: dict[str, str] = d.get("channel_addresses", {})
            n_channels = form.get("channels", 1)

            if channel_addresses:
                for ch, addr in channel_addresses.items():
                    devices.append(
                        {
                            "uid": f"{d.get('uid', '')}_{ch}",
                            "alias": channel_aliases.get(
                                ch, f"CS-Bus Motor {d.get('uid','')} Ch {ch}"
                            ),
                            "address": addr,
                            "platform": PLATFORM_COVER,
                            "device_class": device_class,
                            "color_space": None,
                            "cct_support": False,
                            "cct_warm": None,
                            "cct_cool": None,
                            "channels": 1,
                            "bus_type": form.get("bus_type", "I"),
                            "type_name": d.get("type", "IMC"),
                            "parent_uid": d.get("uid", ""),
                        }
                    )
            else:
                devices.append(
                    {
                        "uid": d.get("uid", ""),
                        "alias": d.get("alias", f"CS-Bus Motor {d.get('uid','')}"),
                        "address": d.get("address", "1.1.0"),
                        "platform": PLATFORM_COVER,
                        "device_class": device_class,
                        "color_space": None,
                        "cct_support": False,
                        "cct_warm": None,
                        "cct_cool": None,
                        "channels": n_channels,
                        "bus_type": form.get("bus_type", "I"),
                        "type_name": d.get("type", "IMC"),
                    }
                )

    return devices


def _parse_form(form_str: str) -> dict[str, Any]:
    """Parse FORM=channels,bus,type,colorspace,cct string."""
    parts = [p.strip() for p in form_str.split(",")]
    result: dict[str, Any] = {}
    if len(parts) >= 1:
        try:
            result["channels"] = int(parts[0])
        except ValueError:
            result["channels"] = 1
    if len(parts) >= 2:
        result["bus_type"] = parts[1].upper()
    if len(parts) >= 3:
        result["type"] = parts[2].upper()
    if len(parts) >= 4:
        result["color_space"] = parts[3].upper()
    if len(parts) >= 5:
        result["cct_support"] = parts[4].upper() == "TRUE"
    return result


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class ENodeCoordinator(DataUpdateCoordinator):  # type: ignore[misc]
    """
    Manages state for all CS-Bus devices.

    Handles both:
    - NOTIFY push messages (Change of Value) from the e-Node
    - Periodic polling as fallback
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: ENodeClient,
        devices: list[dict[str, Any]],
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.devices = devices
        self.firmware_version: str | None = None
        # State cache: address -> {brightness, r, g, b, h, s, v, cct, sun, is_on, position}
        self._state: dict[str, dict[str, Any]] = {}

    def get_state(self, address: str) -> dict[str, Any]:
        """Return the current cached state for a device address."""
        return self._state.get(address, {})

    def handle_message(self, line: str) -> None:
        """Parse and cache an inbound e-Node message, then signal update."""
        if not line.startswith("!"):
            return

        # LED.VALUE=R.G.B  or  LED.VALUE=W (monochrome)
        m = re.match(r"^!(.+)\.LED\.VALUE=(.+);?$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2)
            parts = val.split(".")
            state = self._state.setdefault(addr, {})
            if len(parts) >= 3:
                state["r"] = int(parts[0])
                state["g"] = int(parts[1])
                state["b"] = int(parts[2])
                if len(parts) == 4:
                    state["w"] = int(parts[3])
            elif len(parts) == 1:
                state["brightness_raw"] = int(parts[0])
            state["is_on"] = any(int(p) > 0 for p in parts)
            self.async_set_updated_data(dict(self._state))
            return

        # LED.COLOR=H.S.V
        m = re.match(r"^!(.+)\.LED\.COLOR=(.+);?$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2)
            parts = val.split(".")
            if len(parts) == 3:
                state = self._state.setdefault(addr, {})
                state["h"] = int(parts[0])
                state["s"] = int(parts[1])
                state["v"] = int(parts[2])
                state["is_on"] = int(parts[2]) > 0
                self.async_set_updated_data(dict(self._state))
            return

        # LED.STATUS=sun,cct  (for CCT / circadian devices)
        m = re.match(r"^!(.+)\.LED\.STATUS=(.+);?$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2)
            parts = val.split(",")
            if len(parts) >= 2:
                state = self._state.setdefault(addr, {})
                try:
                    state["sun"] = int(parts[0])
                    state["cct"] = int(parts[1])
                except ValueError:
                    pass
                self.async_set_updated_data(dict(self._state))
            return

        # MOTOR.POSITION=xx.xx
        m = re.match(r"^!(.+)\.MOTOR\.POSITION=(.+);?$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2)
            try:
                state = self._state.setdefault(addr, {})
                state["position"] = float(val)
                self.async_set_updated_data(dict(self._state))
            except ValueError:
                pass
            return

        # MOTOR.STATUS=OPEN/CLOSE/STOP/HOME/EXTENDING/RETRACTING
        m = re.match(r"^!(.+)\.MOTOR\.STATUS=(.+);?$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2).upper()
            state = self._state.setdefault(addr, {})
            state["motor_status"] = val
            self.async_set_updated_data(dict(self._state))
            return

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll all devices for current state."""
        if not self.client.is_connected:
            raise UpdateFailed("e-Node not connected")

        for dev in self.devices:
            addr = dev["address"]
            if dev["platform"] == PLATFORM_LIGHT:
                color_space = dev.get("color_space", "MONO")
                if color_space == "HSV":
                    val = await self.client.async_query(addr, "LED", "COLOR")
                    if val:
                        parts = val.split(".")
                        if len(parts) == 3:
                            state = self._state.setdefault(addr, {})
                            state["h"] = int(parts[0])
                            state["s"] = int(parts[1])
                            state["v"] = int(parts[2])
                            state["is_on"] = int(parts[2]) > 0
                else:
                    val = await self.client.async_query(addr, "LED", "VALUE")
                    if val:
                        parts = val.split(".")
                        state = self._state.setdefault(addr, {})
                        if len(parts) >= 1:
                            state["brightness_raw"] = int(parts[0])
                            state["is_on"] = int(parts[0]) > 0

            elif dev["platform"] == PLATFORM_COVER:
                val = await self.client.async_query(addr, "MOTOR", "POSITION")
                if val:
                    try:
                        state = self._state.setdefault(addr, {})
                        state["position"] = float(val)
                    except ValueError:
                        pass

        return dict(self._state)
