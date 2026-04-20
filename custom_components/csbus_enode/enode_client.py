"""
Converging Systems e-Node / CS-Bus Communication Client.

Supports ALL three bus types via the unified Telnet DISCOVER command:
  - CS-Bus  (ILC-xxx lighting, IMC-xxx motor controllers)  bus type = I
  - DMX     (any DMX512 fixture via e-Node/dmx)            bus type = X
  - DALI    (DALI-TW, DALI-C etc via ILC-DALI controller) bus type = D

CRASH PREVENTION
-----------------
The e-Node/dmx has very limited command bandwidth (~32 DMX fixtures max).
Rules this client MUST follow to avoid crashing the gateway:

1. NEVER send broadcast queries like #0.0.0.LED.VALUE=?
   A broadcast query forces the e-Node to collect responses from ALL
   connected devices simultaneously — this overwhelms the internal bus
   and crashes DMX/DALI gateways.

2. Keepalive must be a NULL/no-op — not a query.
   The e-Node Telnet shell accepts a bare carriage return to keep a
   session alive without triggering any bus traffic.

3. DISCOVER must only be run ONCE at startup — not repeatedly.
   The DISCOVER command walks the entire bus sequentially — running it
   while the system is active would interfere with normal operation.

4. Reconnect must not spawn duplicate tasks.
   Track a _reconnecting flag to prevent concurrent reconnect attempts.

COMMAND PROTOCOL
-----------------
  Send:   #Z.G.N.DEVICE=COMMAND;\r\n
  Query:  #Z.G.N.DEVICE.ITEM=?;\r\n  (specific address only, never 0.0.0)
  Positive response: !Z.G.N.DEVICE.ITEM=value
  Negative response: *... (partial echo — command rejected)

DISCOVER PROTOCOL
------------------
  Send:   >DISCOVER\r\n
  Recv:   +UID101\r\n, !UID101.TYPE=..., !UID101.FORM=..., !DONE,N
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import time
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

TELNET_PORT      = 23
CONNECT_TIMEOUT  = 10.0
COMMAND_TIMEOUT  = 5.0
# Keepalive: send a bare \r\n — keeps TCP session alive, zero bus traffic
KEEPALIVE_INTERVAL = 30.0
# DALI buses are slow to enumerate — give plenty of time
DISCOVER_TIMEOUT = 25.0
RECONNECT_DELAY  = 10.0

# Bus type constants (FORM field position 1)
BUS_CSBUS = "I"
BUS_DMX   = "X"
BUS_DALI  = "D"

# How many outbound commands to keep in the crash-diagnosis ring buffer
CMD_HISTORY_SIZE = 50

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
                i += 3
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
    Split receive buffer into complete messages and a leftover fragment.

    Handles all e-Node line terminator variants:
      \\r\\n        — DISCOVER shell output
      ;\\r\\n       — CS-Bus NOTIFY/query responses
      ;\\r or ;\\n  — older firmware
    """
    messages: list[str] = []
    text = buf.decode("ascii", errors="ignore")
    parts = re.split(r";?\r\n|;\r(?!\n)|;\n", text)
    for part in parts[:-1]:
        cleaned = part.strip().rstrip(";").strip()
        if cleaned:
            messages.append(cleaned)
    return messages, parts[-1].encode("ascii", errors="ignore")


class ENodeClient:
    """
    Async Telnet client for the Converging Systems e-Node gateway.

    Gateway-safe design:
    - Keepalive sends bare \\r\\n — zero bus traffic
    - Never broadcasts queries to 0.0.0 addresses
    - Reconnect is guarded against concurrent attempts
    - DISCOVER runs once at startup only
    """

    def __init__(
        self,
        host: str,
        port: int = TELNET_PORT,
        username: str = "Telnet 1",
        password: str = "Password 1",
        crash_log_dir: str | None = None,
    ) -> None:
        self.host     = host
        self.port     = port
        self.username = username
        self.password = password
        # Directory where crash dump files are written (survives HA restarts).
        # Set to hass.config.config_dir by async_setup_entry.
        self._crash_log_dir = crash_log_dir

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected    = False
        self._reconnecting = False   # guard against concurrent reconnects
        self._listeners: list[Callable[[str], None]] = []
        self._recv_task:      asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._firmware_year: str = ""
        # Set True for CS-Bus gateways only; wildcard NOTIFY is re-sent on reconnect.
        # NEVER set for DMX (causes 44 Hz flood crash) or DALI (not supported).
        self._notify_enabled: bool = False
        # Ring buffer of the last CMD_HISTORY_SIZE outbound commands.
        # Each entry: (wall_time_float, sequence_int, raw_command_str).
        # Dumped to log at WARNING level whenever the connection drops so we can
        # pinpoint exactly which command (or command count) caused the crash.
        self._cmd_seq: int = 0
        self._cmd_history: collections.deque = collections.deque(
            maxlen=CMD_HISTORY_SIZE
        )

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

    async def async_enable_notify(self) -> None:
        """
        Enable wildcard NOTIFY push for a CS-Bus gateway.

        Sends two wildcard commands that instruct the e-Node to push state
        changes for ALL connected CS-Bus lights and motors automatically.
        The flag is stored and the commands are re-sent on every reconnect.

        Call ONLY for pure CS-Bus gateways — NEVER for DMX or DALI:
          - DMX: NOTIFY causes a 44 Hz flood that crashes the firmware.
          - DALI: NOTIFY is not supported by ILC-DALI firmware.
        """
        self._notify_enabled = True
        await self._send_raw("#0.0.0.LED.NOTIFY=VALUE;\r\n")
        await self._send_raw("#0.0.0.MOTOR.NOTIFY=ON;\r\n")

    async def async_disable_notify(self) -> None:
        """
        Explicitly disable NOTIFY push for non-CS-Bus gateways.

        Sends NOTIFY=NONE/OFF to cancel any NOTIFY that may have been left
        active by an older HA session (before the DMX guard was added).
        A leftover active NOTIFY on a DMX gateway causes a 44 Hz push flood
        that eventually crashes the e-Node firmware.
        """
        await self._send_raw("#0.0.0.LED.NOTIFY=NONE;\r\n")
        await self._send_raw("#0.0.0.MOTOR.NOTIFY=OFF;\r\n")

    async def async_connect(self) -> bool:
        """Open Telnet connection and authenticate. Returns True on success."""
        # Prevent re-entrant connects
        if self._connected:
            return True
        try:
            _LOGGER.debug("Connecting to e-Node %s:%s", self.host, self.port)
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
            await self._authenticate()
            self._connected = True
            self._reconnecting = False
            self._recv_task = asyncio.create_task(
                self._receive_loop(), name=f"enode_recv_{self.host}"
            )
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name=f"enode_keepalive_{self.host}"
            )
            # Re-send wildcard NOTIFY for CS-Bus gateways on every connect/reconnect
            if self._notify_enabled:
                await self._send_raw("#0.0.0.LED.NOTIFY=VALUE;\r\n")
                await self._send_raw("#0.0.0.MOTOR.NOTIFY=ON;\r\n")
            _LOGGER.info(
                "e-Node connected at %s (firmware: %s)",
                self.host, self._firmware_year or "unknown",
            )
            return True
        except (OSError, asyncio.TimeoutError) as exc:
            _LOGGER.error(
                "e-Node connection failed (%s): %s(%s)",
                self.host, type(exc).__name__, exc or "no detail",
            )
            self._connected = False
            return False

    async def async_disconnect(self) -> None:
        """Cleanly tear down the connection."""
        self._connected = False
        self._reconnecting = False
        for task in (self._recv_task, self._keepalive_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
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
        Send a CS-Bus command to a SPECIFIC address (never wildcard 0.0.0).
        Format: #Z.G.N.DEVICE=COMMAND;\\r\\n
        """
        if "0.0.0" in zgn:
            _LOGGER.warning("Refusing broadcast command to %s — would flood gateway", zgn)
            return False
        return await self._send_raw(f"#{zgn}.{device}={command};\r\n")

    async def async_send_item_command(
        self, zgn: str, device: str, item: str, value: str
    ) -> bool:
        """Send: #Z.G.N.DEVICE.ITEM=VALUE;\\r\\n"""
        if "0.0.0" in zgn:
            _LOGGER.warning("Refusing broadcast item command to %s", zgn)
            return False
        return await self._send_raw(f"#{zgn}.{device}.{item}={value};\r\n")

    async def async_query(self, zgn: str, device: str, item: str) -> str | None:
        """
        Query a SPECIFIC device for a value. Never use wildcard addresses.
        Sends:    #Z.G.N.DEVICE.ITEM=?;\\r\\n
        Returns:  value string, or None on timeout.
        """
        if "0.0.0" in zgn:
            _LOGGER.warning("Refusing broadcast query to %s — would flood gateway", zgn)
            return None
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
        Run >DISCOVER once to enumerate all CS-Bus / DMX / DALI devices.
        Must only be called once at startup — not during normal operation.
        """
        if not self._connected:
            _LOGGER.warning("DISCOVER: not connected")
            return []

        devices_raw: dict[str, dict] = {}
        done_event = asyncio.Event()

        def _on_msg(line: str) -> None:
            line = line.strip().rstrip(";").strip()
            if not line:
                return

            # +UID101
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

            # !UID300.A.ALIAS=SCREEN  (motor channel alias A-D)
            # !UID101.1.ALIAS=Fixture 1  (DALI fixture alias, numeric index)
            m = re.match(r"^!UID(\w+)\.([A-D]|\d+)\.ALIAS=(.+)$", line, re.IGNORECASE)
            if m:
                d = devices_raw.setdefault(m.group(1), {"uid": m.group(1)})
                d.setdefault("channel_aliases", {})[m.group(2).upper()] = m.group(3).strip()
                return

            # !UID300.A.BUS.ADDRESS=1.1.1  (motor channel address A-D)
            # !UID101.1.BUS.ADDRESS=2.1.1  (DALI fixture address, numeric index)
            m = re.match(r"^!UID(\w+)\.([A-D]|\d+)\.BUS\.ADDRESS=(.+)$", line, re.IGNORECASE)
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
                _LOGGER.debug("DISCOVER !DONE — %s device(s)", m.group(1))
                done_event.set()
                return

        remove = self.add_listener(_on_msg)
        try:
            await self._send_raw(">DISCOVER\r\n")
            try:
                await asyncio.wait_for(done_event.wait(), timeout=DISCOVER_TIMEOUT)
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "DISCOVER: no !DONE after %ss — using %d device(s) so far",
                    DISCOVER_TIMEOUT, len(devices_raw),
                )
        finally:
            remove()

        result = [
            nd for d in devices_raw.values()
            if d.get("uid")
            for nd in [_normalise_device(d)]
            if nd is not None
        ]
        _LOGGER.info("e-Node DISCOVER: %d device(s) at %s", len(result), self.host)
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

    def _log_cmd_history(self, trigger: str) -> None:
        """
        Dump the outbound command history to the HA log AND a persistent file.

        The file survives HA restarts so we can diagnose crashes that happened
        in a prior session. Each crash appends to:
            <config_dir>/csbus_enode_crash_<host>.log
        """
        header = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"e-Node {self.host}: connection lost after {self._cmd_seq} "
            f"total command(s). Trigger: {trigger}. "
            f"Last {len(self._cmd_history)} command(s):\n"
        )
        lines = [
            f"  [#{seq:04d} {time.strftime('%H:%M:%S', time.localtime(ts))}] {cmd}\n"
            for ts, seq, cmd in self._cmd_history
        ]
        separator = "-" * 72 + "\n"

        # Always write to HA log
        _LOGGER.warning(header.rstrip())
        for line in lines:
            _LOGGER.warning(line.rstrip())

        # Write / append to persistent crash log file
        if self._crash_log_dir:
            safe_host = self.host.replace(".", "_").replace(":", "_")
            path = os.path.join(self._crash_log_dir, f"csbus_enode_crash_{safe_host}.log")
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(separator)
                    f.write(header)
                    f.writelines(lines)
                _LOGGER.warning("e-Node crash dump written to %s", path)
            except OSError as exc:
                _LOGGER.warning("Could not write crash dump to %s: %s", path, exc)

    async def _send_raw(self, msg: str) -> bool:
        async with self._lock:
            if not self._writer or self._writer.is_closing():
                _LOGGER.warning("e-Node not connected — dropped: %s", msg.strip())
                return False
            try:
                display = msg.strip() or "[keepalive]"
                self._cmd_seq += 1
                self._cmd_history.append((time.time(), self._cmd_seq, display))
                self._writer.write(msg.encode("ascii", errors="ignore"))
                await self._writer.drain()
                _LOGGER.debug("e-Node %s TX [#%04d]: %s", self.host, self._cmd_seq, display)
                return True
            except (OSError, ConnectionResetError) as exc:
                _LOGGER.warning("e-Node send error: %s(%s)", type(exc).__name__, exc)
                self._log_cmd_history(f"send error: {type(exc).__name__}({exc})")
                self._connected = False
                asyncio.create_task(self._reconnect())
                return False

    async def _authenticate(self) -> None:
        """
        Handle Telnet login following the DDK auth sequence:
          ← User:\r\n
          → username\r\n
          ← Password:\r\n
          → password\r\n
          ← Connected:\r\n   (must arrive before any commands are sent)
        """
        assert self._reader and self._writer

        async def _read_until(keywords: list[str], timeout: float) -> str:
            """Read data until any keyword appears or timeout expires."""
            buf = b""
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                try:
                    chunk = await asyncio.wait_for(self._reader.read(512), timeout=1.5)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                buf += _strip_telnet_negotiation(chunk)
                text = buf.decode("ascii", errors="ignore").lower()
                if any(kw in text for kw in keywords):
                    break
            return buf.decode("ascii", errors="ignore")

        banner = await _read_until(["user:", "connected:"], timeout=6.0)
        _LOGGER.debug("e-Node banner: %r", banner[:200])

        m = re.search(r'\b(20\d{2})\b', banner)
        if m:
            self._firmware_year = m.group(1)

        if "user:" in banner.lower():
            _LOGGER.debug("e-Node requesting credentials")
            self._writer.write(f"{self.username}\r\n".encode())
            await self._writer.drain()
            # Wait for "Password:" before sending password
            await _read_until(["password:"], timeout=4.0)
            self._writer.write(f"{self.password}\r\n".encode())
            await self._writer.drain()
            # Wait for "Connected:" before allowing commands
            await _read_until(["connected:"], timeout=4.0)

    async def _receive_loop(self) -> None:
        """Read from the socket, split into messages, dispatch to listeners."""
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
                    _LOGGER.debug("e-Node %s RX: %s", self.host, msg)
                    self._dispatch(msg)
            except asyncio.TimeoutError:
                continue
            except (OSError, ConnectionResetError) as exc:
                _LOGGER.warning("e-Node receive error: %s(%s)", type(exc).__name__, exc)
                self._log_cmd_history(f"receive error: {type(exc).__name__}({exc})")
                break

        self._connected = False
        if not self._reconnecting:
            self._log_cmd_history("remote closed connection")
        asyncio.create_task(self._reconnect())

    def _dispatch(self, line: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(line)
            except Exception:
                _LOGGER.exception("Listener callback error")

    async def _keepalive_loop(self) -> None:
        """
        Keep the Telnet session alive with a bare carriage return.

        CRITICAL: Do NOT send any CS-Bus queries here.
        A wildcard query like #0.0.0.LED.VALUE=? forces the e-Node to
        poll ALL connected devices simultaneously and will crash DMX/DALI
        gateways. A bare \\r\\n is sufficient to keep the TCP session alive
        and generates zero bus traffic.
        """
        while self._connected:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if self._connected:
                # Bare CR/LF — keeps TCP session alive, zero bus traffic
                await self._send_raw("\r\n")

    async def _reconnect(self) -> None:
        """Re-establish a dropped connection. Guarded against concurrent calls."""
        if self._reconnecting:
            return
        self._reconnecting = True
        self._connected = False

        # Cancel existing tasks cleanly
        for task in (self._recv_task, self._keepalive_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
            self._reader = None

        _LOGGER.info("e-Node %s: reconnecting in %ss…", self.host, RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)
        self._reconnecting = False
        await self.async_connect()


# ---------------------------------------------------------------------------
# Device normalisation
# ---------------------------------------------------------------------------

def _parse_form(form_str: str) -> dict[str, Any]:
    """
    Parse FORM capability string: "channels,bus,class,colorspace,cct"

    Examples:
      "0,I,LIGHT,HSV,TRUE"   — ILC full-color CS-Bus with CCT
      "0,X,LIGHT,HSV,FALSE"  — DMX full-color fixture
      "0,D,LIGHT,MONO,TRUE"  — DALI tunable-white
      "1,I,MOTOR,0,0"        — IMC-100 single-channel motor
      "4,I,LIGHT,MONO,FALSE" — ILC-400m 4-channel monochrome
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
    """
    uid       = str(raw.get("uid", ""))
    alias     = str(raw.get("alias", f"Device {uid}")).strip()
    type_name = str(raw.get("type", "")).strip()

    # Validate ZGN address: must be Z.G.N with all-numeric parts and no octet > 99.
    # Addresses like "172.16.1" are IP address fragments from misconfigured gateways.
    # Devices with no BUS.ADDRESS at all are accepted only if channel_addresses will
    # provide per-entity addresses (e.g. IMC-300 multi-channel motor).
    _raw_address = raw.get("address")
    if _raw_address is None:
        # No BUS.ADDRESS in DISCOVER response — only keep if channel addresses exist
        address = ""
    else:
        address = str(_raw_address).strip().rstrip(".")
        _addr_parts = address.split(".")
        if (
            len(_addr_parts) != 3
            or not all(p.isdigit() for p in _addr_parts)
            or any(int(p) > 99 for p in _addr_parts)
        ):
            _LOGGER.warning(
                "DISCOVER: UID%s has invalid ZGN address %r — "
                "looks like an IP address fragment. Skipping device.",
                uid, address,
            )
            return None

    form_str = raw.get("form", "")
    if form_str:
        form = _parse_form(form_str)
    else:
        # No FORM — infer from type name so DALI/DMX devices aren't misclassified
        is_motor = any(x in type_name.upper() for x in ("IMC", "MOTOR", "BRIC"))
        bus_type = (
            BUS_DALI  if "DALI" in type_name.upper() else
            BUS_DMX   if "DMX"  in type_name.upper() else
            BUS_CSBUS
        )
        form = {
            "channels":     0,
            "bus_type":     bus_type,
            "device_class": "MOTOR" if is_motor else "LIGHT",
            "color_space":  "MONO"  if is_motor else "HSV",
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

    # Preserve per-channel address map for any multi-channel device.
    # Validate each channel address with the same ZGN rule as the main address
    # so IP-address fragments (e.g. 172.16.1) don't leak through as entities.
    if raw.get("channel_addresses"):
        raw_aliases = raw.get("channel_aliases", {})
        valid_ch: dict[str, str] = {}
        valid_al: dict[str, str] = {}
        for ch, ch_addr in raw["channel_addresses"].items():
            parts = ch_addr.split(".")
            if (len(parts) == 3
                    and all(p.isdigit() for p in parts)
                    and all(int(p) <= 99 for p in parts)):
                valid_ch[ch] = ch_addr
                if ch in raw_aliases:
                    valid_al[ch] = raw_aliases[ch]
            else:
                _LOGGER.warning(
                    "DISCOVER: UID%s channel %s address %r is not a valid ZGN — skipping",
                    uid, ch, ch_addr,
                )
        if valid_ch:
            desc["channel_addresses"] = valid_ch
            desc["channel_aliases"]   = valid_al

    elif form["bus_type"] == BUS_DALI and form["channels"] > 0:
        # DALI fallback: DISCOVER didn't return per-fixture !UID.N.BUS.ADDRESS lines.
        # Generate fixture addresses from the base ZGN + DALI short address (1-based).
        # e.g. controller at 2.1.1 with 16 fixtures → 2.1.1 … 2.1.16
        parts = address.split(".")
        if len(parts) == 3:
            z, g = parts[0], parts[1]
            ch_addrs = {str(i): f"{z}.{g}.{i}" for i in range(1, form["channels"] + 1)}
            ch_aliases = raw.get("channel_aliases", {})
            desc["channel_addresses"] = ch_addrs
            desc["channel_aliases"]   = ch_aliases
            _LOGGER.debug(
                "DALI UID%s: no per-fixture addresses in DISCOVER — "
                "generated %d address(es) from base %s (channels=%d)",
                uid, form["channels"], address, form["channels"],
            )

    # If there was no BUS.ADDRESS and no valid channel addresses were produced,
    # the device has no addressable entities — discard it.
    if not address and not desc.get("channel_addresses"):
        _LOGGER.debug(
            "DISCOVER: UID%s has no BUS.ADDRESS and no valid channel addresses — skipping",
            uid,
        )
        return None

    return desc
