"""
Converging Systems e-Node / CS-Bus Communication Client.

Supports both Telnet (TCP port 23, recommended) and UDP (port 5000/4000).

Protocol:  ASCII text, terminated with semicolon + carriage-return.
  Command:  #Z.G.N.DEVICE=COMMAND;\r
  Query:    #Z.G.N.DEVICE.ITEM=?;\r
  Positive response:  !Z.G.N.DEVICE...
  Negative response:  *Z.G.N... (echo of bad command)

Factory defaults:
  Lighting devices ZGN: 2.1.0 (node 0 = wildcard)
  Motor devices ZGN:    1.1.0
  Telnet: TCP port 23, user "Telnet 1", password "Password 1"
  UDP send: port 5000, UDP receive: port 4000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp

_LOGGER = logging.getLogger(__name__)

TELNET_PORT = 23
UDP_SEND_PORT = 5000
UDP_RECV_PORT = 4000
CONNECT_TIMEOUT = 10.0
COMMAND_TIMEOUT = 5.0
KEEPALIVE_INTERVAL = 30.0  # seconds between keepalive pings
RECONNECT_DELAY = 5.0
DISCOVER_TIMEOUT = 20.0  # DALI buses enumerate sequentially — needs longer window

# Response category prefixes
CAT_POSITIVE = "!"   # positive / unsolicited
CAT_NEGATIVE = "*"   # command error echo
CAT_COMMAND  = "#"   # outbound (echoed back)

# Telnet negotiation bytes to strip
_IAC = 0xFF
_TELNET_CMDS = {0xFB, 0xFC, 0xFD, 0xFE}  # WILL/WONT/DO/DONT


def _strip_telnet_negotiation(data: bytes) -> bytes:
    """Remove IAC negotiation sequences from raw telnet bytes."""
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == _IAC and i + 1 < len(data):
            if data[i + 1] in _TELNET_CMDS and i + 2 < len(data):
                i += 3  # skip IAC CMD OPTION
                continue
            elif data[i + 1] == _IAC:
                out.append(_IAC)
                i += 2
                continue
        out.append(data[i])
        i += 1
    return bytes(out)


def _split_messages(buf: bytes) -> tuple[list[str], bytes]:
    """
    Split receive buffer into complete messages and a leftover fragment.
    Messages end with: \\r\\n, ;\\r\\n, ;\\r, or ;\\n
    Returns (list_of_clean_strings, remaining_bytes).
    """
    messages: list[str] = []
    text = buf.decode("ascii", errors="ignore")
    parts = re.split(r";?\r\n|;\r(?!\n)|;\n", text)
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

    Maintains a persistent TCP connection, handles authentication,
    reconnection, and dispatches inbound NOTIFY messages to listeners.
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
        self._recv_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[str], None]) -> Callable[[], None]:
        """Register a callback invoked for every inbound message line."""
        self._listeners.append(callback)

        def _remove() -> None:
            self._listeners.remove(callback)

        return _remove

    async def async_connect(self) -> bool:
        """Open connection and authenticate. Returns True on success."""
        try:
            _LOGGER.debug("Connecting to e-Node %s:%s", self.host, self.port)
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
            await self._authenticate()
            self._connected = True
            self._recv_task = asyncio.create_task(self._receive_loop())
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            _LOGGER.info("Connected to e-Node at %s", self.host)
            return True
        except (TimeoutError, OSError) as exc:
            _LOGGER.error("e-Node connection failed: %s", exc)
            self._connected = False
            return False

    async def async_disconnect(self) -> None:
        """Cleanly close the connection."""
        self._connected = False
        if self._recv_task:
            self._recv_task.cancel()
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._writer:
            with contextlib.suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None

    async def async_send_command(self, zgn: str, device: str, command: str) -> bool:
        """
        Send a command: #Z.G.N.DEVICE=COMMAND;\\r\\n
        e.g. async_send_command("2.1.1", "LED", "ON")
        """
        msg = f"#{zgn}.{device}={command};\r\n"
        return await self._send_raw(msg)

    async def async_send_item_command(
        self, zgn: str, device: str, item: str, command: str
    ) -> bool:
        """
        Send a command with item: #Z.G.N.DEVICE.ITEM=COMMAND;\\r\\n
        e.g. async_send_item_command("2.1.1", "LED", "DISSOLVE.1", "3")
        """
        msg = f"#{zgn}.{device}.{item}={command};\r\n"
        return await self._send_raw(msg)

    async def async_query(self, zgn: str, device: str, item: str) -> str | None:
        """
        Send a query and return the response value string, or None on timeout.
        e.g. async_query("2.1.1", "LED", "COLOR") -> "120.200.180"
        """
        msg = f"#{zgn}.{device}.{item}=?;\r\n"
        response_event = asyncio.Event()
        response_value: list[str] = []

        # Match positive response; trailing ';' already stripped by _split_messages
        pattern = re.compile(
            rf"^!{re.escape(zgn)}\.{re.escape(device)}\.{re.escape(item)}=(.+)",
            re.IGNORECASE,
        )

        def _on_message(line: str) -> None:
            m = pattern.match(line)
            if m:
                response_value.append(m.group(1).strip())
                response_event.set()

        remove = self.add_listener(_on_message)
        try:
            await self._send_raw(msg)
            await asyncio.wait_for(response_event.wait(), timeout=COMMAND_TIMEOUT)
            return response_value[0] if response_value else None
        except TimeoutError:
            _LOGGER.debug("Query timeout for %s.%s.%s", zgn, device, item)
            return None
        finally:
            remove()

    async def async_discover(self) -> list[dict[str, Any]]:
        """
        Run the e-Node DISCOVER command and parse all device info.
        Returns a list of device dicts with keys:
          uid, type, form, alias, address, cct_warm, cct_cool
        """
        if not self._connected:
            return []

        devices_raw: dict[str, dict[str, Any]] = {}
        done_event = asyncio.Event()

        def _on_msg(line: str) -> None:
            # +UID102  -> newly found device
            m = re.match(r"^\+UID(\w+)$", line, re.IGNORECASE)
            if m:
                uid = m.group(1)
                if uid not in devices_raw:
                    devices_raw[uid] = {"uid": uid}
                return

            # !UID102.TYPE=ILC400CE
            m = re.match(r"^!UID(\w+)\.TYPE=(.+)$", line, re.IGNORECASE)
            if m:
                uid, val = m.group(1), m.group(2)
                devices_raw.setdefault(uid, {"uid": uid})["type"] = val
                return

            # !UID102.FORM=0,X,LIGHT,HSV,FALSE
            m = re.match(r"^!UID(\w+)\.FORM=(.+)$", line, re.IGNORECASE)
            if m:
                uid, val = m.group(1), m.group(2)
                devices_raw.setdefault(uid, {"uid": uid})["form"] = val
                return

            # !UID102.ALIAS=THEATER LIGHTS
            m = re.match(r"^!UID(\w+)\.ALIAS=(.+)$", line, re.IGNORECASE)
            if m:
                uid, val = m.group(1), m.group(2)
                devices_raw.setdefault(uid, {"uid": uid})["alias"] = val
                return

            # !UID102.A.ALIAS=SCREEN  (motor channel alias)
            m = re.match(r"^!UID(\w+)\.([A-D])\.ALIAS=(.+)$", line, re.IGNORECASE)
            if m:
                uid, ch, val = m.group(1), m.group(2).upper(), m.group(3)
                d = devices_raw.setdefault(uid, {"uid": uid})
                d.setdefault("channel_aliases", {})[ch] = val
                return

            # !UID102.BUS.ADDRESS=4.1.2
            m = re.match(r"^!UID(\w+)\.BUS\.ADDRESS=(.+)$", line, re.IGNORECASE)
            if m:
                uid, val = m.group(1), m.group(2)
                devices_raw.setdefault(uid, {"uid": uid})["address"] = val
                return

            # !UID102.A.BUS.ADDRESS=1.1.1  (motor channel address)
            m = re.match(r"^!UID(\w+)\.([A-D])\.BUS\.ADDRESS=(.+)$", line, re.IGNORECASE)
            if m:
                uid, ch, val = m.group(1), m.group(2).upper(), m.group(3)
                d = devices_raw.setdefault(uid, {"uid": uid})
                d.setdefault("channel_addresses", {})[ch] = val
                return

            # !UID102.LED.CMS.WARM=1700
            m = re.match(r"^!UID(\w+)\.LED\.CMS\.WARM=(\d+)$", line, re.IGNORECASE)
            if m:
                uid, val = m.group(1), int(m.group(2))
                devices_raw.setdefault(uid, {"uid": uid})["cct_warm"] = val
                return

            # !UID102.LED.CMS.COOL=6500
            m = re.match(r"^!UID(\w+)\.LED\.CMS\.COOL=(\d+)$", line, re.IGNORECASE)
            if m:
                uid, val = m.group(1), int(m.group(2))
                devices_raw.setdefault(uid, {"uid": uid})["cct_cool"] = val
                return

            # !DONE,n  -> discovery complete
            if re.match(r"^!DONE,\d+$", line, re.IGNORECASE):
                done_event.set()

        remove = self.add_listener(_on_msg)
        try:
            await self._send_raw(">DISCOVER\r\n")
            # Wait up to DISCOVER_TIMEOUT; e-Node may not send !DONE on older firmware
            try:
                await asyncio.wait_for(done_event.wait(), timeout=DISCOVER_TIMEOUT)
            except TimeoutError:
                _LOGGER.debug(
                    "DISCOVER: no !DONE after %ss — using %d device(s) collected so far",
                    DISCOVER_TIMEOUT,
                    len(devices_raw),
                )
        finally:
            remove()

        return list(devices_raw.values())

    async def async_fetch_firmware_version(
        self, session: aiohttp.ClientSession
    ) -> str | None:
        """Fetch firmware version string from the e-Node web interface."""
        import re  # noqa: PLC0415

        try:
            async with session.get(
                f"http://{self.host}/",
                timeout=5,
            ) as resp:
                text = await resp.text()
            for pattern in (
                r"[Ff]irmware[^\d]*(\d+[\.\d]+)",
                r"[Vv]ersion[^\d]*(\d+[\.\d]+)",
            ):
                m = re.search(pattern, text)
                if m:
                    return m.group(1)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not fetch firmware version from web interface")
        return None

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_raw(self, msg: str) -> bool:
        async with self._lock:
            if not self._writer or self._writer.is_closing():
                _LOGGER.warning("e-Node: not connected, cannot send: %s", msg.strip())
                return False
            try:
                self._writer.write(msg.encode())
                await self._writer.drain()
                _LOGGER.debug("e-Node TX: %s", msg.strip())
                return True
            except (OSError, ConnectionResetError) as exc:
                _LOGGER.warning("e-Node send error: %s", exc)
                self._connected = False
                asyncio.create_task(self._reconnect())
                return False

    async def _authenticate(self) -> None:
        """Handle plaintext Telnet login if the e-Node prompts for it."""
        assert self._reader and self._writer
        # Read initial banner; handle IAC negotiation silently
        buf = b""
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(self._reader.read(256), timeout=1.0)
            except TimeoutError:
                break
            buf += _strip_telnet_negotiation(chunk)
            text = buf.decode("ascii", errors="ignore").lower()
            if "user" in text or "connected" in text:
                break

        text = buf.decode("ascii", errors="ignore")
        if "User" in text or "user" in text:
            _LOGGER.debug("e-Node requesting authentication")
            self._writer.write(f"{self.username}\r\n".encode())
            await self._writer.drain()
            # wait for Password prompt
            await asyncio.sleep(0.5)
            self._writer.write(f"{self.password}\r\n".encode())
            await self._writer.drain()
            # consume "Connected:" line
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._reader.readline(), timeout=3.0)

    async def _receive_loop(self) -> None:
        """Continuously read lines from the e-Node and dispatch to listeners."""
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
            except TimeoutError:
                continue
            except (OSError, ConnectionResetError) as exc:
                _LOGGER.warning("e-Node receive error: %s", exc)
                break

        self._connected = False
        asyncio.create_task(self._reconnect())

    def _dispatch(self, line: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(line)
            except Exception as exc:
                _LOGGER.exception("Listener error: %s", exc)

    async def _keepalive_loop(self) -> None:
        """Send a periodic no-op to keep the Telnet session alive."""
        while self._connected:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if self._connected:
                # Query the e-Node firmware version as a keepalive
                await self._send_raw("#0.0.0.LED.VALUE=?;\r\n")

    async def _reconnect(self) -> None:
        """Attempt to re-establish connection after a disconnect."""
        self._connected = False
        if self._recv_task:
            self._recv_task.cancel()
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._writer:
            with contextlib.suppress(Exception):
                self._writer.close()
        _LOGGER.info("e-Node: reconnecting in %ss…", RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)
        await self.async_connect()
