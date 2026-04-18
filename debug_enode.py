#!/usr/bin/env python3
"""
Raw Telnet debug script for Converging Systems e-Node gateways.

Usage:
    python3 debug_enode.py <host> [port]

Connects, authenticates, sends >DISCOVER, captures all responses for 25 s,
then sends a few manual LED commands and captures those responses too.
All raw bytes are printed with repr() so no data is hidden.
"""

from __future__ import annotations

import asyncio
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 23
# Override with env vars if needed: DEBUG_USER / DEBUG_PASS
import os
USERNAME = os.environ.get("DEBUG_USER", "Telnet 1")
PASSWORD = os.environ.get("DEBUG_PASS", "Password 1")


def ts() -> str:
    return f"{time.monotonic():.3f}"


async def raw_recv(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Read everything available within *timeout* seconds."""
    buf = b""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=min(remaining, 0.5))
            if not chunk:
                break
            buf += chunk
        except asyncio.TimeoutError:
            # No data in the last 0.5 s — keep trying until outer deadline
            continue
    return buf


async def main() -> None:
    print(f"[{ts()}] Connecting to {HOST}:{PORT} …")
    reader, writer = await asyncio.open_connection(HOST, PORT)
    print(f"[{ts()}] TCP connected")

    # ---------------------------------------------------------------
    # Capture banner / Telnet negotiation (up to 4 s)
    # ---------------------------------------------------------------
    banner = await raw_recv(reader, 4.0)
    print(f"\n[{ts()}] === BANNER ({len(banner)} bytes) ===")
    print(repr(banner))
    banner_text = banner.decode("ascii", errors="replace").lower()

    # ---------------------------------------------------------------
    # Authenticate if prompted
    # ---------------------------------------------------------------
    if "user" in banner_text:
        print(f"\n[{ts()}] Auth prompt detected — sending username: {USERNAME!r}")
        writer.write(f"{USERNAME}\r\n".encode())
        await writer.drain()
        # Wait for "Password:" prompt (up to 3 s)
        pw_buf = b""
        deadline2 = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline2:
            try:
                chunk = await asyncio.wait_for(reader.read(512), timeout=0.5)
                if not chunk:
                    break
                pw_buf += chunk
                print(f"[{ts()}] AFTER_USER_RX: {pw_buf!r}")
                if b"assword" in pw_buf or b"onnect" in pw_buf:
                    break
            except asyncio.TimeoutError:
                continue
        writer.write(f"{PASSWORD}\r\n".encode())
        await writer.drain()
        print(f"[{ts()}] TX >>> {PASSWORD!r}")
        auth_resp = await raw_recv(reader, 3.0)
        print(f"[{ts()}] AUTH RESPONSE: {auth_resp!r}")
    else:
        print(f"[{ts()}] No auth prompt — continuing unauthenticated")

    # ---------------------------------------------------------------
    # Send >DISCOVER and capture for 25 s
    # ---------------------------------------------------------------
    discover_cmd = b">DISCOVER\r\n"
    writer.write(discover_cmd)
    await writer.drain()
    print(f"\n[{ts()}] TX >>> {discover_cmd!r}")
    print(f"[{ts()}] Capturing DISCOVER response for 25 s …")

    discover_buf = b""
    deadline = asyncio.get_event_loop().time() + 25.0
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=min(remaining, 0.5))
            if not chunk:
                print(f"[{ts()}] Connection closed by remote during DISCOVER")
                break
            discover_buf += chunk
            # Print each chunk as it arrives
            print(f"[{ts()}] CHUNK ({len(chunk)} bytes): {chunk!r}")
        except asyncio.TimeoutError:
            continue

    print(f"\n[{ts()}] === FULL DISCOVER BUFFER ({len(discover_buf)} bytes) ===")
    print(repr(discover_buf))
    print(f"\n[{ts()}] === DISCOVER LINE-BY-LINE ===")
    for i, line in enumerate(discover_buf.split(b"\n")):
        print(f"  line {i:03d}: {line!r}")

    # ---------------------------------------------------------------
    # Enable NOTIFY (same as the integration does on connect)
    # ---------------------------------------------------------------
    print(f"\n[{ts()}] === ENABLING NOTIFY ===")
    for notify_cmd in [
        b"#0.0.0.LED.NOTIFY=VALUE;\r\n",
        b"#0.0.0.MOTOR.NOTIFY=ON;\r\n",
    ]:
        writer.write(notify_cmd)
        await writer.drain()
        print(f"[{ts()}] TX >>> {notify_cmd!r}")
        resp = await raw_recv(reader, 1.5)
        print(f"[{ts()}] NOTIFY RX: {resp!r}")
        await asyncio.sleep(0.3)

    # ---------------------------------------------------------------
    # Send test LED commands and capture responses
    # ---------------------------------------------------------------
    test_cmds = [
        # ON/OFF
        ("ILC-DALI ON",        b"#1.16.0.LED=ON;\r\n"),
        ("ILC-DALI OFF",       b"#1.16.0.LED=OFF;\r\n"),
        # Brightness
        ("VALUE=120",          b"#1.16.0.LED.VALUE=120;\r\n"),
        ("VALUE=?",            b"#1.16.0.LED.VALUE=?;\r\n"),
        # CCT candidates
        ("SUN=120",            b"#1.16.0.LED.SUN=120;\r\n"),
        ("SUN=?",              b"#1.16.0.LED.SUN=?;\r\n"),
        ("CCT=3000",           b"#1.16.0.LED.CCT=3000;\r\n"),
        ("CCT=?",              b"#1.16.0.LED.CCT=?;\r\n"),
        ("SET=120,60",         b"#1.16.0.LED.SET=120,60;\r\n"),
        ("SET=120,60 ?",       b"#1.16.0.LED.SET=?;\r\n"),
        # STATUS
        ("STATUS=?",           b"#1.16.0.LED.STATUS=?;\r\n"),
        # Individual DALI node 1 — same tests
        ("node1 ON",           b"#1.16.1.LED=ON;\r\n"),
        ("node1 VALUE=120",    b"#1.16.1.LED.VALUE=120;\r\n"),
        ("node1 VALUE=?",      b"#1.16.1.LED.VALUE=?;\r\n"),
        ("node1 SUN=120",      b"#1.16.1.LED.SUN=120;\r\n"),
        ("node1 OFF",          b"#1.16.1.LED=OFF;\r\n"),
        # Nodes 2-5 ON/OFF (check if multiple work)
        ("node2 ON",           b"#1.16.2.LED=ON;\r\n"),
        ("node2 OFF",          b"#1.16.2.LED=OFF;\r\n"),
        ("node14 ON",          b"#1.16.14.LED=ON;\r\n"),
        ("node14 OFF",         b"#1.16.14.LED=OFF;\r\n"),
        # Node 15+ — should not exist
        ("node15 ON",          b"#1.16.15.LED=ON;\r\n"),
    ]

    print(f"\n[{ts()}] === MANUAL COMMAND TESTS ===")
    for label, cmd in test_cmds:
        writer.write(cmd)
        await writer.drain()
        print(f"[{ts()}] [{label}] TX >>> {cmd!r}")
        resp = await raw_recv(reader, 2.5)
        print(f"[{ts()}] [{label}] RX <<< {resp!r}")
        await asyncio.sleep(0.3)

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    print(f"\n[{ts()}] Done.")


if __name__ == "__main__":
    asyncio.run(main())
