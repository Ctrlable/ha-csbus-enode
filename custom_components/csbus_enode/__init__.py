"""
Converging Systems e-Node CS-Bus integration for Home Assistant.

Architecture
------------
One config entry = one e-Node gateway (one Telnet connection).
Multiple e-Nodes = multiple config entries.

Device model
------------
DISCOVER returns one entry per CONTROLLABLE ADDRESS on the bus:
  - CS-Bus:  one UID per ILC/IMC controller
  - DMX:     one UID per DMX fixture (up to 32 per e-Node/dmx)
  - DALI:    one ILC-DALI controller UID; each DALI fixture has its
             own ZGN address (zone.group.DALI_address, e.g. 2.1.1..2.1.64)

State updates
-------------
The integration uses NOTIFY push (listen mode) as primary — the e-Node
broadcasts state changes on the bus automatically after any command.
Polling is used only as a fallback and is deliberately rate-limited to
avoid flooding the gateway (especially critical for DMX/DALI).

Poll strategy:
  - Polling is DISABLED by default (NOTIFY handles everything).
  - When polling is enabled (configurable), we query ONE device every
    POLL_STAGGER_DELAY seconds rather than all at once.
  - DALI buses are never polled individually — they report via NOTIFY only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_DEVICES,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    PLATFORM_COVER,
    PLATFORM_LIGHT,
)
from .enode_client import ENodeClient, BUS_CSBUS, BUS_DALI, BUS_DMX

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT, Platform.COVER]

# Time between individual device polls to avoid flooding the gateway.
# At 0.5 s/device, 30 DMX fixtures = 15 s total — well within a 30 s interval.
POLL_STAGGER_DELAY = 0.5   # seconds between each device query

# DALI and DMX gateways must never be flooded — if the scan_interval is
# very short, clamp it to this minimum.
MIN_SCAN_INTERVAL_DMX  = 60   # seconds
MIN_SCAN_INTERVAL_DALI = 60
MIN_SCAN_INTERVAL_CSBUS = 30


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the e-Node integration from a config entry."""
    host     = entry.data["host"]
    port     = entry.data.get(CONF_PORT, DEFAULT_PORT)
    username = entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)
    password = entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD)
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    client = ENodeClient(host=host, port=port, username=username, password=password)

    if not await client.async_connect():
        _LOGGER.error("Failed to connect to e-Node at %s", host)
        return False

    # Run DISCOVER — returns normalised device dicts from enode_client
    discovered = await client.async_discover()
    _LOGGER.debug("e-Node raw discovery: %d device(s)", len(discovered))

    # _parse_devices maps the normalised dicts to HA platform descriptors
    devices = _parse_devices(discovered)

    n_lights = sum(1 for d in devices if d["platform"] == PLATFORM_LIGHT)
    n_covers = sum(1 for d in devices if d["platform"] == PLATFORM_COVER)
    _LOGGER.info(
        "e-Node %s: %d device(s) — %d light(s), %d cover(s)",
        host, len(devices), n_lights, n_covers,
    )

    # Enable wildcard NOTIFY only for pure CS-Bus gateways.
    # A single wildcard command covers all devices on the bus.
    # DMX is excluded — NOTIFY causes a 44 Hz flood that crashes the firmware.
    # DALI is excluded — NOTIFY is not supported by ILC-DALI firmware.
    bus_types = {d.get("bus_type", "I") for d in devices}
    if bus_types == {BUS_CSBUS}:
        await client.async_enable_notify()
        _LOGGER.debug("e-Node %s: wildcard NOTIFY enabled (CS-Bus gateway)", host)
    else:
        _LOGGER.debug(
            "e-Node %s: NOTIFY skipped — bus types present: %s",
            host, bus_types,
        )

    # Clamp scan_interval based on the bus types present
    if BUS_DMX in bus_types:
        scan_interval = max(scan_interval, MIN_SCAN_INTERVAL_DMX)
    if BUS_DALI in bus_types:
        scan_interval = max(scan_interval, MIN_SCAN_INTERVAL_DALI)

    coordinator = ENodeCoordinator(hass, client, devices, scan_interval)

    # Register NOTIFY listener — all state updates arrive here for free
    remove_listener = client.add_listener(coordinator.handle_notify)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
        DATA_DEVICES: devices,
        "remove_listener": remove_listener,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the integration and close the Telnet connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        data.get("remove_listener", lambda: None)()
        await data[DATA_CLIENT].async_disconnect()
    return unloaded


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


# ---------------------------------------------------------------------------
# Device parsing
# ---------------------------------------------------------------------------

def _parse_devices(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Convert normalised DISCOVER dicts (from enode_client._normalise_device)
    into HA platform descriptors.

    enode_client already handles the FORM parsing and normalisation.
    Here we just map platform="light"/"cover" and handle multi-channel motors.

    Key mapping (enode_client output → what we store):
      platform     → "light" or "cover"
      device_class → "LIGHT" or "MOTOR"
      uid          → unique_id suffix
      alias        → entity name
      address      → ZGN string for commands
      color_space  → "HSV" or "MONO"
      cct_support  → bool
      bus_type     → "I", "X", or "D"
    """
    devices: list[dict[str, Any]] = []

    for d in raw:
        platform     = d.get("platform", "light")
        device_class = d.get("device_class", "LIGHT")

        if platform == PLATFORM_LIGHT:
            channel_addresses = d.get("channel_addresses", {})
            channel_aliases   = d.get("channel_aliases", {})

            if channel_addresses:
                # ILC-DALI multi-channel or similar: one light entity per channel
                # Mirrors the IMC-300 motor pattern — same !UID.X.BUS.ADDRESS protocol
                for ch, addr in channel_addresses.items():
                    devices.append({
                        "uid":          f"{d['uid']}_{ch}",
                        "alias":        channel_aliases.get(ch, f"{d.get('alias', 'Light')} Ch {ch}"),
                        "address":      addr,
                        "platform":     PLATFORM_LIGHT,
                        "device_class": device_class,
                        "color_space":  d.get("color_space", "MONO"),
                        "cct_support":  d.get("cct_support", False),
                        "cct_warm":     d.get("cct_warm", 2700),
                        "cct_cool":     d.get("cct_cool", 6500),
                        "channels":     1,
                        "bus_type":     d.get("bus_type", "I"),
                        "type_name":    d.get("type_name", "ILC"),
                        "parent_uid":   d["uid"],
                    })
            else:
                devices.append({
                    "uid":          d["uid"],
                    "alias":        d.get("alias", f"Light {d['uid']}"),
                    "address":      d.get("address", "2.1.1"),
                    "platform":     PLATFORM_LIGHT,
                    "device_class": device_class,
                    "color_space":  d.get("color_space", "MONO"),
                    "cct_support":  d.get("cct_support", False),
                    "cct_warm":     d.get("cct_warm", 2700),
                    "cct_cool":     d.get("cct_cool", 6500),
                    "channels":     d.get("channels", 1),
                    "bus_type":     d.get("bus_type", "I"),
                    "type_name":    d.get("type_name", "ILC"),
                })

        elif platform == PLATFORM_COVER:
            channel_addresses = d.get("channel_addresses", {})
            channel_aliases   = d.get("channel_aliases", {})

            if channel_addresses:
                # IMC-300 multi-channel: one cover entity per channel
                for ch, addr in channel_addresses.items():
                    devices.append({
                        "uid":         f"{d['uid']}_{ch}",
                        "alias":       channel_aliases.get(ch, f"{d.get('alias', 'Motor')} Ch {ch}"),
                        "address":     addr,
                        "platform":    PLATFORM_COVER,
                        "device_class": device_class,
                        "color_space": None,
                        "cct_support": False,
                        "cct_warm":    None,
                        "cct_cool":    None,
                        "channels":    1,
                        "bus_type":    d.get("bus_type", "I"),
                        "type_name":   d.get("type_name", "IMC"),
                        "parent_uid":  d["uid"],
                    })
            else:
                devices.append({
                    "uid":         d["uid"],
                    "alias":       d.get("alias", f"Motor {d['uid']}"),
                    "address":     d.get("address", "1.1.1"),
                    "platform":    PLATFORM_COVER,
                    "device_class": device_class,
                    "color_space": None,
                    "cct_support": False,
                    "cct_warm":    None,
                    "cct_cool":    None,
                    "channels":    d.get("channels", 1),
                    "bus_type":    d.get("bus_type", "I"),
                    "type_name":   d.get("type_name", "IMC"),
                })

        else:
            _LOGGER.debug("Skipping unknown platform device: %s", d)

    return devices


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class ENodeCoordinator(DataUpdateCoordinator):
    """
    State coordinator for all CS-Bus/DMX/DALI devices on one e-Node.

    Primary method: NOTIFY push (listen mode).
      The e-Node sends unsolicited state updates after any command.
      No polling needed for devices with NOTIFY enabled.

    Fallback: Staggered polling.
      Queries devices one at a time with a delay between each to avoid
      flooding the gateway — critical for DMX (max 32 fixtures) and
      DALI (max 64 fixtures) which have limited command bandwidth.
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
            name=f"{DOMAIN}_{client.host}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client  = client
        self.devices = devices
        # address → state dict
        self._state: dict[str, dict[str, Any]] = {}
        # Index for staggered polling — rotate through devices one per cycle
        self._poll_index = 0

    def get_state(self, address: str) -> dict[str, Any]:
        return self._state.get(address, {})

    # ------------------------------------------------------------------
    # NOTIFY handler — called for every inbound Telnet message
    # ------------------------------------------------------------------

    def handle_notify(self, line: str) -> None:
        """
        Parse an inbound e-Node message and update state cache.

        Only '!' (positive/unsolicited) messages carry state.
        '#' echoes and '*' errors are ignored.
        """
        line = line.strip().rstrip(";")

        # Command echoes — the e-Node echoes every accepted command back with a '#'
        # prefix, regardless of the source (web UI, keypad, Telnet, CS-Bus event).
        # Parse ON/OFF echoes so external state changes are reflected in HA.
        # The optional (SOURCE) suffix e.g. (TELNET), (WEB), (CSBUS) is ignored.
        if line.startswith("#"):
            # ON
            m = re.match(r"^#(.+?)\.LED=ON(?:\([^)]*\))?$", line, re.IGNORECASE)
            if m:
                self._state.setdefault(m.group(1), {})["is_on"] = True
                self.async_set_updated_data(dict(self._state))
                return
            # OFF
            m = re.match(r"^#(.+?)\.LED=OFF(?:\([^)]*\))?$", line, re.IGNORECASE)
            if m:
                self._state.setdefault(m.group(1), {})["is_on"] = False
                self.async_set_updated_data(dict(self._state))
                return
            # SET,N — dim level (0–240)
            m = re.match(r"^#(.+?)\.LED=SET,(\d+)(?:\([^)]*\))?$", line, re.IGNORECASE)
            if m:
                level = int(m.group(2))
                self._state.setdefault(m.group(1), {}).update(
                    brightness_raw=level, v=level, is_on=level > 0
                )
                self.async_set_updated_data(dict(self._state))
                return
            # HSV,H.S.V
            m = re.match(
                r"^#(.+?)\.LED=HSV,(\d+)\.(\d+)\.(\d+)(?:\([^)]*\))?$",
                line, re.IGNORECASE,
            )
            if m:
                v = int(m.group(4))
                self._state.setdefault(m.group(1), {}).update(
                    h=int(m.group(2)), s=int(m.group(3)), v=v, is_on=v > 0
                )
                self.async_set_updated_data(dict(self._state))
                return
            # CCT,XXXX (Kelvin)
            m = re.match(r"^#(.+?)\.LED=CCT,(\d+)(?:\([^)]*\))?$", line, re.IGNORECASE)
            if m:
                self._state.setdefault(m.group(1), {}).update(
                    cct=int(m.group(2)), is_on=True
                )
                self.async_set_updated_data(dict(self._state))
                return
            return  # ignore all other # echoes

        if not line.startswith("!"):
            return

        changed = False

        # !Z.G.N.LED.VALUE=R.G.B  or  =W (mono)  or  =R.G.B.W (RGBW)
        m = re.match(r"^!(.+?)\.LED\.VALUE=(.+)$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2).rstrip(";")
            state = self._state.setdefault(addr, {})
            parts = val.split(".")
            try:
                if len(parts) >= 4:
                    state.update(r=int(parts[0]), g=int(parts[1]),
                                 b=int(parts[2]), w=int(parts[3]))
                elif len(parts) == 3:
                    state.update(r=int(parts[0]), g=int(parts[1]), b=int(parts[2]))
                elif len(parts) == 2:
                    # Bi-white (ILC-200E / DALI-TW): VALUE=warm_channel.cool_channel
                    state.update(warm=int(parts[0]), cool=int(parts[1]))
                elif len(parts) == 1:
                    state["brightness_raw"] = int(parts[0])
                state["is_on"] = any(int(p) > 0 for p in parts)
                changed = True
            except (ValueError, IndexError):
                pass

        # !Z.G.N.LED.COLOR=H.S.V
        m = re.match(r"^!(.+?)\.LED\.COLOR=(.+)$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2).rstrip(";")
            parts = val.split(".")
            if len(parts) == 3:
                state = self._state.setdefault(addr, {})
                try:
                    state.update(h=int(parts[0]), s=int(parts[1]), v=int(parts[2]))
                    state["is_on"] = int(parts[2]) > 0
                    changed = True
                except (ValueError, IndexError):
                    pass

        # !Z.G.N.LED.STATUS=sun_val,cct_val  (CCT/circadian devices)
        m = re.match(r"^!(.+?)\.LED\.STATUS=(.+)$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2).rstrip(";")
            parts = val.split(",")
            state = self._state.setdefault(addr, {})
            try:
                if len(parts) >= 2:
                    state["sun"] = int(parts[0])
                    state["cct"] = int(parts[1])
                    changed = True
            except (ValueError, IndexError):
                pass

        # !Z.G.N.MOTOR.POSITION=xx.xx
        m = re.match(r"^!(.+?)\.MOTOR\.POSITION=(.+)$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2).rstrip(";")
            try:
                self._state.setdefault(addr, {})["position"] = float(val)
                changed = True
            except ValueError:
                pass

        # !Z.G.N.MOTOR.STATUS=OPEN/CLOSE/STOP/HOME/EXTENDING/RETRACTING
        m = re.match(r"^!(.+?)\.MOTOR\.STATUS=(.+)$", line, re.IGNORECASE)
        if m:
            addr, val = m.group(1), m.group(2).rstrip(";").upper()
            self._state.setdefault(addr, {})["motor_status"] = val
            changed = True

        if changed:
            self.async_set_updated_data(dict(self._state))

    # ------------------------------------------------------------------
    # Staggered polling fallback
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """
        Poll ONE device per coordinator update cycle (staggered polling).

        This prevents the gateway from being overwhelmed.
        DALI devices are skipped — they only support NOTIFY push.
        """
        if not self.client.is_connected:
            raise UpdateFailed("e-Node not connected")

        # Skip DMX and DALI — both crash or misbehave under individual polling.
        # CS-Bus devices with NOTIFY don't need polling either, but it serves
        # as a useful fallback for devices that missed a NOTIFY push.
        pollable = [
            d for d in self.devices
            if d.get("bus_type", "I") not in (BUS_DALI, BUS_DMX)
        ]

        if not pollable:
            return dict(self._state)

        # Rotate index — poll one device per cycle
        self._poll_index = self._poll_index % len(pollable)
        dev = pollable[self._poll_index]
        self._poll_index += 1

        addr = dev["address"]
        try:
            if dev["platform"] == PLATFORM_LIGHT:
                color_space = dev.get("color_space", "MONO")
                if color_space == "HSV":
                    val = await self.client.async_query(addr, "LED", "COLOR")
                    if val:
                        parts = val.rstrip(";").split(".")
                        if len(parts) == 3:
                            state = self._state.setdefault(addr, {})
                            state.update(
                                h=int(parts[0]),
                                s=int(parts[1]),
                                v=int(parts[2]),
                                is_on=int(parts[2]) > 0,
                            )
                else:
                    val = await self.client.async_query(addr, "LED", "VALUE")
                    if val:
                        parts = val.rstrip(";").split(".")
                        state = self._state.setdefault(addr, {})
                        try:
                            state["brightness_raw"] = int(parts[0])
                            state["is_on"] = int(parts[0]) > 0
                        except (ValueError, IndexError):
                            pass

            elif dev["platform"] == PLATFORM_COVER:
                val = await self.client.async_query(addr, "MOTOR", "POSITION")
                if val:
                    try:
                        self._state.setdefault(addr, {})["position"] = float(val.rstrip(";"))
                    except ValueError:
                        pass

        except Exception as exc:
            _LOGGER.debug("Poll error for %s (%s): %s", dev.get("alias"), addr, exc)

        return dict(self._state)
