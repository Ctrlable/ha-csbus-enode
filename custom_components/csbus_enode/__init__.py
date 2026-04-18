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
  - DALI:    one ILC-DALI controller UID whose ZGN address covers all
             individual DALI nodes (e.g. 1.16.0 = broadcast, 1.16.1-64
             = individual fixtures)

DISCOVER failure — DALI bus crash
----------------------------------
When the DALI bus crashes the e-Node gateway stays online (its network
stack is independent of the DALI bus layer) but immediately returns
!DONE,0 with no device data.  This is the primary cause of DISCOVER
returning 0 devices on this installation — not a commissioning gap or
a polling race.

When manual_nodes is configured, device entities are created from the
known ZGN address range regardless of DISCOVER outcome.  Commands are
queued through the persistent Telnet session and take effect as soon as
the DALI bus recovers, with no HA restart required.

State updates — per bus type
-----------------------------
CS-Bus:
  NOTIFY push is available — the e-Node broadcasts !Z.G.N.LED.VALUE /
  !Z.G.N.LED.COLOR messages after every command.  Staggered polling is
  the fallback.  NOTIFY is NOT enabled by this component (enode_client
  never sends the wildcard NOTIFY enable) to keep the code safe across
  all gateway types.

DMX:
  NOTIFY is never enabled — it would flood the gateway at 44 Hz per
  fixture and crash the firmware.  Staggered polling is the only
  mechanism, clamped to MIN_SCAN_INTERVAL_DMX.

DALI (this installation):
  The e-Node firmware does NOT send NOTIFY push for DALI buses.  DALI
  query responses echo back as '#' messages (not '!' push), so
  async_query always times out.  State is maintained exclusively through:
    1. Optimistic updates written immediately after each command.
    2. Command echoes: #Z.G.N.LED=ON(TELNET) / OFF(TELNET) parsed by
       handle_notify to confirm ON/OFF transitions.
  Polling is intentionally skipped for DALI — it would only produce
  COMMAND_TIMEOUT noise on every coordinator cycle.
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
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_MANUAL_NODES,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_DEVICES,
    DEFAULT_MANUAL_NODES,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    PLATFORM_COVER,
    PLATFORM_LIGHT,
)
from .enode_client import ENodeClient, BUS_DALI, BUS_DMX

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

    # If DISCOVER found nothing, fall back to manually configured nodes.
    #
    # Primary cause on this installation: the DALI bus crashes at startup
    # while the e-Node gateway stays online, causing !DONE,0 with no device
    # data.  Manual nodes bypass DISCOVER entirely so entities always exist
    # regardless of bus state.  A secondary cause is devices not yet
    # commissioned with UIDs in e-Node Pilot.
    if not discovered:
        manual_spec = entry.options.get(
            CONF_MANUAL_NODES,
            entry.data.get(CONF_MANUAL_NODES, DEFAULT_MANUAL_NODES),
        )
        if manual_spec and manual_spec.strip():
            discovered = _make_manual_devices(manual_spec)
            _LOGGER.info(
                "e-Node %s: DISCOVER returned 0 devices — using %d manual node(s) from config",
                host, len(discovered),
            )

    # _parse_devices maps the normalised dicts to HA platform descriptors
    devices = _parse_devices(discovered)

    n_lights = sum(1 for d in devices if d["platform"] == PLATFORM_LIGHT)
    n_covers = sum(1 for d in devices if d["platform"] == PLATFORM_COVER)
    _LOGGER.info(
        "e-Node %s: %d device(s) — %d light(s), %d cover(s)",
        host, len(devices), n_lights, n_covers,
    )

    # Determine bus types present across all devices.
    bus_types = {d.get("bus_type", "I") for d in devices}

    # Clamp scan_interval based on bus types present.
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
            devices.append({
                "uid":         d["uid"],
                "alias":       d.get("alias", f"Light {d['uid']}"),
                "address":     d.get("address", "2.1.1"),
                "platform":    PLATFORM_LIGHT,
                "device_class": device_class,
                "color_space": d.get("color_space", "MONO"),
                "cct_support": d.get("cct_support", False),
                "cct_warm":    d.get("cct_warm", 2700),
                "cct_cool":    d.get("cct_cool", 6500),
                "channels":    d.get("channels", 1),
                "bus_type":    d.get("bus_type", "I"),
                "type_name":   d.get("type_name", "ILC"),
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


def _parse_manual_nodes(spec: str) -> list[dict[str, Any]]:
    """
    Parse a manual node spec string into a list of node descriptor dicts.

    Token format (comma or space separated, mixable):
      RANGE[:bus[:colorspace[:cct[:warm_k[:cool_k]]]]]

    Address / range formats:
      Z.G.N          single ZGN address
      Z.G.start-end  inclusive address range

    Capability fields (all optional, positional after the address):
      bus        D = DALI (default), X = DMX, I = CS-Bus
      colorspace MONO (default), HSV
      cct        0 = no CCT (default), 1 = tunable-white / CCT
      warm_k     warm end in Kelvin (default 2700; ignored when cct=0)
      cool_k     cool end in Kelvin (default 6500; ignored when cct=0)

    Examples:
      "1.16.1-14"                 DALI mono dimmers
      "2.1.1-8:X:HSV"             DMX full-colour RGB fixtures
      "2.1.9-12:X:MONO:1"         DMX tunable-white (CCT)
      "2.1.9-12:X:MONO:1:3000:6500"  DMX CCT with custom Kelvin range
      "3.1.1:I:HSV:1"             CS-Bus full-colour + CCT
    """
    _BUS_NAMES = {"D": BUS_DALI, "X": BUS_DMX, "I": "I"}
    nodes: list[dict[str, Any]] = []

    for token in re.split(r"[,\s]+", spec.strip()):
        token = token.strip()
        if not token:
            continue

        parts = token.split(":")
        addr_part = parts[0]

        bus_raw = parts[1].upper() if len(parts) > 1 and parts[1] else "D"
        bus = _BUS_NAMES.get(bus_raw, BUS_DALI)

        colorspace = parts[2].upper() if len(parts) > 2 and parts[2] else "MONO"
        cct_support = len(parts) > 3 and parts[3] == "1"

        try:
            cct_warm = int(parts[4]) if len(parts) > 4 and parts[4] else 2700
        except ValueError:
            cct_warm = 2700
        try:
            cct_cool = int(parts[5]) if len(parts) > 5 and parts[5] else 6500
        except ValueError:
            cct_cool = 6500

        # Expand range or accept a single address
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)-(\d+)$", addr_part)
        if m:
            z, g, s, e = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            addresses = [f"{z}.{g}.{n}" for n in range(s, e + 1)]
        elif re.match(r"^\d+\.\d+\.\d+$", addr_part):
            addresses = [addr_part]
        else:
            _LOGGER.warning("Ignoring unrecognised manual node token: %r", token)
            continue

        for addr in addresses:
            nodes.append({
                "addr":        addr,
                "bus":         bus,
                "colorspace":  colorspace,
                "cct_support": cct_support,
                "cct_warm":    cct_warm,
                "cct_cool":    cct_cool,
            })

    return nodes


def _make_manual_devices(spec: str) -> list[dict[str, Any]]:
    """
    Build synthetic normalised device descriptors from a manual node spec.

    Used as a fallback when DISCOVER returns 0 devices.  Supports DALI, DMX,
    and CS-Bus fixtures with any combination of color space and CCT capability.
    """
    _BUS_TYPE_NAMES = {BUS_DALI: "ILC-DALI", BUS_DMX: "e-Node/DMX", "I": "ILC"}
    devices: list[dict[str, Any]] = []

    for node in _parse_manual_nodes(spec):
        addr = node["addr"]
        bus  = node["bus"]
        safe = addr.replace(".", "_")
        devices.append({
            "uid":          f"manual_{safe}",
            "alias":        f"Light {addr}",
            "address":      addr,
            "platform":     PLATFORM_LIGHT,
            "device_class": "LIGHT",
            "color_space":  node["colorspace"],
            "cct_support":  node["cct_support"],
            "cct_warm":     node["cct_warm"],
            "cct_cool":     node["cct_cool"],
            "channels":     0 if node["colorspace"] == "HSV" else 1,
            "bus_type":     bus,
            "type_name":    _BUS_TYPE_NAMES.get(bus, bus),
        })

    return devices


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class ENodeCoordinator(DataUpdateCoordinator):
    """
    State coordinator for all CS-Bus/DMX/DALI devices on one e-Node.

    CS-Bus / DMX — NOTIFY push (primary):
      The e-Node sends unsolicited !Z.G.N.LED.VALUE / COLOR messages after
      any command.  Staggered polling (one device per cycle) is the fallback.

    DALI — optimistic + echo-based (only mechanism available):
      This firmware does not send NOTIFY push for DALI buses, and query
      responses echo back as '#' messages rather than '!' push, so neither
      NOTIFY nor polling returns actual device state.  State is maintained
      via optimistic writes in light.py and #Z.G.N.LED=ON/OFF(TELNET) echoes
      parsed in handle_notify.  DALI is excluded from the polling loop to
      avoid COMMAND_TIMEOUT noise on every update cycle.
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

        '!' messages carry authoritative state (CS-Bus NOTIFY push).
        '#Z.G.N.LED=ON(TELNET)' echoes are produced by DALI firmware when a
        command is executed on a specific node — parse these for ON/OFF state.
        All other '#' echoes and '*' acknowledgements are ignored.
        """
        line = line.strip().rstrip(";")

        # DALI firmware echoes: #Z.G.N.LED=ON(TELNET) / #Z.G.N.LED=OFF(TELNET)
        m = re.match(r"^#(.+?)\.LED=ON\(TELNET\)$", line, re.IGNORECASE)
        if m:
            self._state.setdefault(m.group(1), {})["is_on"] = True
            self.async_set_updated_data(dict(self._state))
            return

        m = re.match(r"^#(.+?)\.LED=OFF\(TELNET\)$", line, re.IGNORECASE)
        if m:
            self._state.setdefault(m.group(1), {})["is_on"] = False
            self.async_set_updated_data(dict(self._state))
            return

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
                    # Bi-white (CCT) device: VALUE=warm_channel.cool_channel
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
        DALI devices are excluded: their query responses echo back as '#'
        commands rather than '!' push messages, so async_query always times
        out — polling them would only generate COMMAND_TIMEOUT noise.
        """
        if not self.client.is_connected:
            raise UpdateFailed("e-Node not connected")

        # Skip DALI — queries echo as '#' not '!', so async_query always times out
        pollable = [
            d for d in self.devices
            if d.get("bus_type", "I") != BUS_DALI
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
