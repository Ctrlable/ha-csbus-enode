"""Tests for ENodeClient (Telnet client layer)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.csbus_enode.enode_client import (
    ENodeClient,
    _strip_telnet_negotiation,
)


# ---------------------------------------------------------------------------
# Pure-function tests — no async, no mocks
# ---------------------------------------------------------------------------


class TestStripTelnetNegotiation:
    def test_strips_iac_do_option(self) -> None:
        # IAC DO NVT_STATUS (0xFF 0xFD 0x18) followed by "HI"
        data = bytes([0xFF, 0xFD, 0x18]) + b"HI"
        assert _strip_telnet_negotiation(data) == b"HI"

    def test_strips_iac_will_option(self) -> None:
        data = bytes([0xFF, 0xFB, 0x01]) + b"OK"
        assert _strip_telnet_negotiation(data) == b"OK"

    def test_preserves_escaped_iac(self) -> None:
        # IAC IAC is an escaped 0xFF literal
        data = bytes([0xFF, 0xFF]) + b"X"
        result = _strip_telnet_negotiation(data)
        assert result == bytes([0xFF]) + b"X"

    def test_plain_ascii_unchanged(self) -> None:
        data = b"hello world"
        assert _strip_telnet_negotiation(data) == b"hello world"

    def test_empty_bytes(self) -> None:
        assert _strip_telnet_negotiation(b"") == b""

    def test_multiple_sequences(self) -> None:
        data = bytes([0xFF, 0xFD, 0x01]) + b"A" + bytes([0xFF, 0xFB, 0x03]) + b"B"
        assert _strip_telnet_negotiation(data) == b"AB"


# ---------------------------------------------------------------------------
# ENodeClient async tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> ENodeClient:
    return ENodeClient(host="192.168.1.100", port=23, username="user", password="pass")


@pytest.mark.asyncio
async def test_connect_success(client: ENodeClient) -> None:
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False

    # _authenticate reads initial banner then writes credentials
    mock_reader.read = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_reader.readline = AsyncMock(return_value=b"Connected: OK\r\n")

    with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
        with patch("asyncio.create_task"):
            result = await client.async_connect()

    assert result is True
    assert client.is_connected is True


@pytest.mark.asyncio
async def test_connect_failure(client: ENodeClient) -> None:
    with patch("asyncio.open_connection", side_effect=OSError("refused")):
        result = await client.async_connect()

    assert result is False
    assert client.is_connected is False


@pytest.mark.asyncio
async def test_connect_timeout(client: ENodeClient) -> None:
    with patch(
        "asyncio.open_connection",
        side_effect=asyncio.TimeoutError,
    ):
        result = await client.async_connect()

    assert result is False


@pytest.mark.asyncio
async def test_send_command_when_connected(client: ENodeClient) -> None:
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.drain = AsyncMock()
    client._writer = mock_writer
    client._connected = True

    result = await client.async_send_command("2.1.1", "LED", "ON")

    assert result is True
    mock_writer.write.assert_called_once_with(b"#2.1.1.LED=ON;\r\n")


@pytest.mark.asyncio
async def test_send_command_when_disconnected(client: ENodeClient) -> None:
    client._connected = False
    client._writer = None

    result = await client.async_send_command("2.1.1", "LED", "ON")
    assert result is False


@pytest.mark.asyncio
async def test_send_item_command(client: ENodeClient) -> None:
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.drain = AsyncMock()
    client._writer = mock_writer
    client._connected = True

    result = await client.async_send_item_command("2.1.1", "LED", "DISSOLVE.1", "3")

    assert result is True
    mock_writer.write.assert_called_once_with(b"#2.1.1.LED.DISSOLVE.1=3;\r\n")


@pytest.mark.asyncio
async def test_disconnect(client: ENodeClient) -> None:
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.wait_closed = AsyncMock()
    mock_recv = MagicMock()
    mock_recv.cancel = MagicMock()
    mock_recv.done.return_value = False
    mock_keep = MagicMock()
    mock_keep.cancel = MagicMock()
    mock_keep.done.return_value = False

    client._writer = mock_writer
    client._connected = True
    client._recv_task = mock_recv
    client._keepalive_task = mock_keep

    await client.async_disconnect()

    assert client.is_connected is False
    mock_recv.cancel.assert_called_once()
    mock_keep.cancel.assert_called_once()


def test_add_and_remove_listener(client: ENodeClient) -> None:
    received: list[str] = []
    remove = client.add_listener(received.append)
    client._dispatch("hello")
    assert received == ["hello"]

    remove()
    client._dispatch("ignored")
    assert received == ["hello"]
