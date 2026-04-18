"""
Converging Systems e-Node / CS-Bus Communication Client.

Supports ALL three bus types via the unified Telnet DISCOVER command:
  - CS-Bus  (ILC-xxx lighting, IMC-xxx motor controllers)  bus type = I
  - DMX     (any DMX512 fixture via e-Node/dmx)            bus type = X
  - DALI    (DALI-TW, DALI-C etc via ILC-DALI controller) bus type = D

DISCOVERY — how it actually works
----------------------------------
The correct Telnet shell command is:   >DISCOVER\r\n   (note the leading '>')
The e-Node replies with a burst of lines like:

  +UID101;\r\n                              <- new device announced
  #UID101.TYPE=?;\r\n                       <- e-Node querying its own bus
  !UID101.TYPE=ILC-DALI;\r\n               <- device type
  !UID101.FORM=0,D,LIGHT,MONO,TRUE;\r\n    <- capabilities
  #UID101.ALIAS=?;\r\n
  !UID101.ALIAS=Main Cove RGBV;\r\n
  #UID101.BUS.ADDRESS=?;\r\n
  !UID101.BUS.ADDRESS=2.1.1;\r\n
  !DONE,5;\r\n                              <- all done

FORM field positions:
  [0] channel count  (0=full-color, 1=mono, 2=bi-white, 3=RGB, 4=RGBW)
  [1] bus type       (I=CS-Bus, X=DMX, D=DALI)
  [2] device class   (LIGHT, MOTOR, KEYPAD)
  [3] color space    (HSV, MONO)
  [4] CCT support    (TRUE / FALSE)

IMPORTANT parser notes
-----------------------
The receive loop splits on \\r\\n (line-based) — NOT only on semicolons.
DISCOVER responses are newline-terminated.
CS-Bus NOTIFY/query responses end with semicolon then newline.
We handle both cleanly in _split_messages().
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

TELNET_PORT = 23
CONNECT_TIMEOUT = 10.0
COMMAND_TIMEOUT = 5.0
KEEPALIVE_INTERVAL = 45.0
RECONNECT_DELAY = 5.0
DISCOVER_TIMEOUT = 30.0   # DALI buses can be slow — live test showed 16 s response time

# Bus type constants (FORM field position 1)
BUS_CSBUS = "I"
BUS_DMX   = "X"
BUS_DALI  = "D"

# Telnet IAC negotiation bytes
_IAC = 0xFF
_TELNET_CMDS = {0xFB, 0xFC, 0xFD, 0xFE}  # WILL / WONT / DO / DONT


def _strip_telnet_negotiation(data: bytes) -> bytes:
    """Remove Telnet IAC option sequences so they don't corrupt messages."""
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == _IAC and i + 1 < len(data):
            nb = data[i + 1]
            if nb in _TELNET_CMDS and i + 2 < len(data):
                i += 3          # skip IAC CMD OPTION
                continue
            elif nb == _IAC:
                out.append(_IAC)
                i += 2
                continue
        out.append(b)
        i += 1
    return bytes(out)


def _split_messages(buf: bytes) -> tuple[list[str], bytes]:
    """
    Split the receive buffer into complete messages and a leftover fragment.

    Messages may end with:
      \\r\\n          — DISCOVER shell responses
      ;\\r\\n         — CS-Bus NOTIFY/query responses (most common)
      ;\\r  or  ;\\n  — older firmware variants

    Returns (list_of_clean_strings, remaining_bytes).
    Each string has surrounding whitespace and trailing semicolons stripped.
    """
    messages: list[str] = []
    # Decode to string for easier splitting
    text = buf.decode("ascii", errors="ignore")

    # Split on any recognised line terminator (semicolon optional before newline)
    parts = re.split(r";?\r\n|;\r(?!\n)|;\n", text)

    # The last element has no terminator yet — it's our leftover fragment
    complete = parts[:-1]
    leftover = parts[-1]

    for part in complete:
        cleaned = part.strip().rstrip(";").strip()
        if cleaned:
            messages.append(cleaned)

    return messages, leftover.encode("ascii", errors="ignore")


class ENodeClient:
    """
    Async Telnet client for the Converging Systems e-Node gateway.

    - Persistent TCP connection with automatic reconnect
    - Plaintext Telnet authentication (optional on e-Node)
    - DISCOVER command enumerates CS-Bus, DMX and DALI fixtures
    - Dispatches NOTIFY push messages to registered listeners
    - Sends CS-Bus ASCII commands (works transparently for all bus types)
    """

    def __init__(
        self,
        host: str,
        port: int = TELNET_PORT,
        username: str = "Telnet 1",
        password: str = "Password 1",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._listeners: list[Callable[[str], None]] = []
        self._recv_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._firmware_year: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[str], None]) -> Callable:
        """Register a callback for every complete inbound message line."""
        self._listeners.append(callback)
        def _remove() -> None:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass
        return _remove

    async def async_connect(self) -> bool:
        """Open Telnet connection and authenticate. Returns True on success."""
        try:
            _LOGGER.debug("Connecting to e-Node %s:%s", self.host, self.port)
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
            await self._authenticate()
            self._connected = True
            self._recv_task = asyncio.create_task(
                self._receive_loop(), name="enode_recv"
            )
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name="enode_keepalive"
            )
            _LOGGER.info("e-Node connected at %s (firmware year: %s)",
                         self.host, self._firmware_year or "unknown")
            return True
        except (OSError, asyncio.TimeoutError) as exc:
            _LOGGER.error("e-Node connection failed (%s): %s", self.host, exc)
            self._connected = False
            return False

    async def async_disconnect(self) -> None:
        """Cleanly tear down the connection."""
        self._connected = False
        for task in (self._recv_task, self._keepalive_task):
            if task and not task.done():
                task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def async_send_command(self, zgn: str, device: str, command: str) -> bool:
        """
        Send a CS-Bus control command.
        Format:  #Z.G.N.DEVICE=COMMAND;\\r\\n
        The e-Node translates automatically for DMX and DALI fixtures.
        """
        return await self._send_raw(f"#{zgn}.{device}={command};\r\n")

    async def async_send_item_command(
        self, zgn: str, device: str, item: str, value: str
    ) -> bool:
        """Send:  #Z.G.N.DEVICE.ITEM=VALUE;\\r\\n"""
        return await self._send_raw(f"#{zgn}.{device}.{item}={value};\r\n")

    async def async_query(self, zgn: str, device: str, item: str) -> str | None:
        """
        Send a query and return the response value, or None on timeout.
        Sends:     #Z.G.N.DEVICE.ITEM=?;\\r\\n
        Receives:  !Z.G.N.DEVICE.ITEM=value;
        """
        msg = f"#{zgn}.{device}.{item}=?;\r\n"
        event = asyncio.Event()
        result: list[str] = []

        pattern = re.compile(
            rf"^!{re.escape(zgn)}\.{re.escape(device)}\.{re.escape(item)}=(.+)$",
            re.IGNORECASE,
        )

        def _on_message(line: str) -> None:
            m = pattern.match(line.rstrip(";"))
            if m:
                result.append(m.group(1).strip().rstrip(";"))
                event.set()

        remove = self.add_listener(_on_message)
        try:
            await self._send_raw(msg)
            await asyncio.wait_for(event.wait(), timeout=COMMAND_TIMEOUT)
            return result[0] if result else None
        except asyncio.TimeoutError:
            _LOGGER.debug("Query timeout: %s.%s.%s", zgn, device, item)
            return None
        finally:
            remove()

    async def async_discover(self) -> list[dict[str, Any]]:
        """
        Run the Telnet DISCOVER command and enumerate all devices.

        Sends:  >DISCOVER\\r\\n
        Waits for !DONE or the DISCOVER_TIMEOUT, then returns every
        device found as a normalised descriptor dict.

        Works for CS-Bus, DMX and DALI — the FORM field identifies each.
        """
        if not self._connected:
            _LOGGER.warning("DISCOVER called but e-Node not connected")
            return []

        devices_raw: dict[str, dict] = {}
        done_event = asyncio.Event()

        def _on_msg(line: str) -> None:
            # Strip trailing semicolons left over from the splitter
            line = line.strip().rstrip(";").strip()
            if not line:
                return

            # +UID101  — new device announced
            m = re.match(r"^\+UID(\w+)$", line, re.IGNORECASE)
            if m:
                uid = m.group(1)
                devices_raw.setdefault(uid, {"uid": uid})
                _LOGGER.debug("DISCOVER +UID%s", uid)
                return

            # !UID101.TYPE=ILC-DALI
            m = re.match(r"^!UID(\w+)\.TYPE=(.+)$", line, re.IGNORECASE)
            if m:
                devices_raw.setdefault(m.group(1), {"uid": m.group(1)})["type"] = m.group(2).strip()
                return

            # !UID101.FORM=0,D,LIGHT,MONO,TRUE
            m = re.match(r"^!UID(\w+)\.FORM=(.+)$", line, re.IGNORECASE)
            if m:
                devices_raw.setdefault(m.group(1), {"uid": m.group(1)})["form"] = m.group(2).strip()
                return

            # !UID101.ALIAS=Main Cove RGBV  (value may be empty)
            m = re.match(r"^!UID(\w+)\.ALIAS=(.*)$", line, re.IGNORECASE)
            if m:
                val = m.group(2).strip()
                if val:
                    devices_raw.setdefault(m.group(1), {"uid": m.group(1)})["alias"] = val
                return

            # !UID101.BUS.ADDRESS=2.1.1
            m = re.match(r"^!UID(\w+)\.BUS\.ADDRESS=(.+)$", line, re.IGNORECASE)
            if m:
                devices_raw.setdefault(m.group(1), {"uid": m.group(1)})["address"] = m.group(2).strip()
                return

            # !UID300.A.ALIAS=SCREEN  (motor channel alias)
            m = re.match(r"^!UID(\w+)\.([A-D])\.ALIAS=(.+)$", line, re.IGNORECASE)
            if m:
                d = devices_raw.setdefault(m.group(1), {"uid": m.group(1)})
                d.setdefault("channel_aliases", {})[m.group(2).upper()] = m.group(3).strip()
                return

            # !UID300.A.BUS.ADDRESS=1.1.1  (motor channel address)
            m = re.match(r"^!UID(\w+)\.([A-D])\.BUS\.ADDRESS=(.+)$", line, re.IGNORECASE)
            if m:
                d = devices_raw.setdefault(m.group(1), {"uid": m.group(1)})
                d.setdefault("channel_addresses", {})[m.group(2).upper()] = m.group(3).strip()
                return

            # !UID101.LED.CMS.WARM=1700
            m = re.match(r"^!UID(\w+)\.LED\.CMS\.WARM=(\d+)$", line, re.IGNORECASE)
            if m:
                devices_raw.setdefault(m.group(1), {"uid": m.group(1)})["cct_warm"] = int(m.group(2))
                return

            # !UID101.LED.CMS.COOL=6500
            m = re.match(r"^!UID(\w+)\.LED\.CMS\.COOL=(\d+)$", line, re.IGNORECASE)
            if m:
                devices_raw.setdefault(m.group(1), {"uid": m.group(1)})["cct_cool"] = int(m.group(2))
                return

            # !DONE,5
            m = re.match(r"^!DONE,(\d+)$", line, re.IGNORECASE)
            if m:
                _LOGGER.debug("DISCOVER !DONE — %s device(s) enumerated", m.group(1))
                done_event.set()
                return

        remove = self.add_listener(_on_msg)
        try:
            # CRITICAL: the correct command is '>DISCOVER' not just 'DISCOVER'
            await self._send_raw(">DISCOVER\r\n")
            try:
                await asyncio.wait_for(done_event.wait(), timeout=DISCOVER_TIMEOUT)
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "DISCOVER: no !DONE after %ss — using %d device(s) collected so far",
                    DISCOVER_TIMEOUT, len(devices_raw),
                )
        finally:
            remove()

        result = [_normalise_device(d) for d in devices_raw.values() if d.get("uid")]
        _LOGGER.info(
            "e-Node DISCOVER complete: %d device(s) found at %s",
            len(result), self.host,
        )
        return result

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def firmware_year(self) -> str:
        return self._firmware_year

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_raw(self, msg: str) -> bool:
        async with self._lock:
            if not self._writer or self._writer.is_closing():
                _LOGGER.warning("e-Node not connected — dropped: %s", msg.strip())
                return False
            try:
                self._writer.write(msg.encode("ascii", errors="ignore"))
                await self._writer.drain()
                _LOGGER.debug("e-Node TX: %s", msg.strip())
                return True
            except (OSError, ConnectionResetError) as exc:
                _LOGGER.warning("e-Node send error: %s", exc)
                self._connected = False
                asyncio.create_task(self._reconnect())
                return False

    async def _authenticate(self) -> None:
        """
        Handle Telnet login if the e-Node has authentication enabled.

        Flow:
          <- User:\r\n
          -> Telnet 1\r\n
          <- Password:\r\n
          -> Password 1\r\n
          <- Connected:\r\n
        """
        assert self._reader and self._writer
        buf = b""
        deadline = asyncio.get_event_loop().time() + 6.0

        while asyncio.get_event_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(self._reader.read(512), timeout=1.5)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buf += _strip_telnet_negotiation(chunk)
            text = buf.decode("ascii", errors="ignore").lower()
            if "user" in text or "connected" in text:
                break

        text_orig = buf.decode("ascii", errors="ignore")
        _LOGGER.debug("e-Node banner: %r", text_orig[:200])

        if "user" in text_orig.lower():
            _LOGGER.debug("e-Node requesting credentials")
            self._writer.write(f"{self.username}\r\n".encode())
            await self._writer.drain()
            await asyncio.sleep(0.6)
            self._writer.write(f"{self.password}\r\n".encode())
            await self._writer.drain()
            try:
                await asyncio.wait_for(self._reader.readline(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

        # Capture firmware year from banner if present (e.g. "e-Node MkIV … 2023")
        m = re.search(r'\b(20\d{2})\b', text_orig)
        if m:
            self._firmware_year = m.group(1)

    async def _receive_loop(self) -> None:
        """Read from the socket, split into clean messages, dispatch to listeners."""
        assert self._reader
        buf = b""

        while self._connected:
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=60.0)
                if not chunk:
                    _LOGGER.warning("e-Node: connection closed by remote")
                    break
                buf += _strip_telnet_negotiation(chunk)
                messages, buf = _split_messages(buf)
                for msg in messages:
                    _LOGGER.debug("e-Node RX: %s", msg)
                    self._dispatch(msg)
            except asyncio.TimeoutError:
                continue  # normal idle; keepalive handles session health
            except (OSError, ConnectionResetError) as exc:
                _LOGGER.warning("e-Node receive error: %s", exc)
                break

        self._connected = False
        asyncio.create_task(self._reconnect())

    def _dispatch(self, line: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(line)
            except Exception:
                _LOGGER.exception("Listener callback raised an exception")

    async def _keepalive_loop(self) -> None:
        """Send a harmless wildcard query periodically to keep the session alive."""
        while self._connected:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if self._connected:
                await self._send_raw("#0.0.0.LED.VALUE=?;\r\n")

    async def _reconnect(self) -> None:
        """Re-establish a dropped connection."""
        self._connected = False
        for task in (self._recv_task, self._keepalive_task):
            if task and not task.done():
                task.cancel()
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
        _LOGGER.info("e-Node: reconnecting in %ss…", RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)
        await self.async_connect()


# ---------------------------------------------------------------------------
# Device normalisation
# ---------------------------------------------------------------------------

def _parse_form(form_str: str) -> dict[str, Any]:
    """
    Parse the FORM capability string: "channels,bus,class,colorspace,cct"

    Real-world examples from DDK Appendix 2:
      "0,I,LIGHT,HSV,TRUE"   — ILC full-color CS-Bus with CCT
      "0,X,LIGHT,HSV,FALSE"  — DMX full-color fixture
      "0,D,LIGHT,MONO,TRUE"  — DALI tunable-white (DALI-TW)
      "1,I,MOTOR,0,0"        — IMC-100 single-channel motor
      "4,I,LIGHT,MONO,FALSE" — ILC-400m 4-channel monochrome
      "2,I,LIGHT,MONO,TRUE"  — ILC-400BE bi-white
    """
    parts = [p.strip() for p in form_str.split(",")]

    def _p(idx: int, default: str = "") -> str:
        return parts[idx] if len(parts) > idx else default

    try:
        channels = int(_p(0, "0"))
    except ValueError:
        channels = 0

    return {
        "channels":     channels,
        "bus_type":     _p(1, BUS_CSBUS).upper(),
        "device_class": _p(2, "LIGHT").upper(),
        "color_space":  _p(3, "HSV").upper(),
        "cct_support":  _p(4, "FALSE").upper() == "TRUE",
    }


def _normalise_device(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a raw DISCOVER response dict into a standard HA device descriptor.
    The same structure is consumed by __init__._parse_devices(), light.py and cover.py.
    """
    uid      = str(raw.get("uid", ""))
    alias    = str(raw.get("alias", f"Device {uid}")).strip()
    address  = str(raw.get("address", "2.1.1")).strip().rstrip(".")
    type_name = str(raw.get("type", "")).strip()

    form_str = raw.get("form", "")
    if form_str:
        form = _parse_form(form_str)
    else:
        # No FORM — infer from type name
        is_motor = any(x in type_name.upper() for x in ("IMC", "MOTOR", "BRIC"))
        form = {
            "channels":     0,
            "bus_type":     BUS_CSBUS,
            "device_class": "MOTOR" if is_motor else "LIGHT",
            "color_space":  "MONO" if is_motor else "HSV",
            "cct_support":  False,
        }

    platform = "cover" if form["device_class"] == "MOTOR" else "light"

    desc: dict[str, Any] = {
        "uid":          uid,
        "alias":        alias,
        "address":      address,
        "platform":     platform,
        "device_class": form["device_class"],
        "color_space":  form["color_space"],
        "cct_support":  form["cct_support"],
        "cct_warm":     raw.get("cct_warm", 2700),
        "cct_cool":     raw.get("cct_cool", 6500),
        "channels":     form["channels"],
        "bus_type":     form["bus_type"],
        "type_name":    type_name or f"e-Node/{form['bus_type']}",
    }

    # Multi-channel motor (IMC-300 with A/B/C/D channels)
    if form["device_class"] == "MOTOR" and raw.get("channel_addresses"):
        desc["channel_addresses"] = raw["channel_addresses"]
        desc["channel_aliases"]   = raw.get("channel_aliases", {})

    return desc
