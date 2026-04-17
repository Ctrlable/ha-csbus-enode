"""Tests for ENodeCoordinator and device-parsing helpers."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.csbus_enode.__init__ import (
    ENodeCoordinator,
    _parse_devices,
    _parse_form,
)


# ---------------------------------------------------------------------------
# _parse_form
# ---------------------------------------------------------------------------


class TestParseForm:
    def test_full_form(self) -> None:
        result = _parse_form("4,I,LIGHT,HSV,TRUE")
        assert result == {
            "channels": 4,
            "bus_type": "I",
            "type": "LIGHT",
            "color_space": "HSV",
            "cct_support": True,
        }

    def test_motor_form(self) -> None:
        result = _parse_form("1,I,MOTOR,MONO,FALSE")
        assert result["type"] == "MOTOR"
        assert result["cct_support"] is False

    def test_partial_form(self) -> None:
        result = _parse_form("2,X")
        assert result["channels"] == 2
        assert result["bus_type"] == "X"
        assert "type" not in result

    def test_empty_form(self) -> None:
        result = _parse_form("")
        assert result == {"channels": 1}

    def test_invalid_channel_count(self) -> None:
        result = _parse_form("abc,I,LIGHT")
        assert result["channels"] == 1

    def test_cct_false(self) -> None:
        result = _parse_form("1,I,LIGHT,MONO,FALSE")
        assert result["cct_support"] is False

    def test_bus_type_uppercased(self) -> None:
        result = _parse_form("1,x,LIGHT")
        assert result["bus_type"] == "X"


# ---------------------------------------------------------------------------
# _parse_devices
# ---------------------------------------------------------------------------


class TestParseDevices:
    def _light_raw(self, uid: str = "101") -> dict[str, Any]:
        return {
            "uid": uid,
            "alias": "Office Lights",
            "address": "2.1.1",
            "type": "ILC400CE",
            "form": "1,I,LIGHT,HSV,TRUE",
            "cct_warm": 2700,
            "cct_cool": 6500,
        }

    def _motor_raw(self, uid: str = "201") -> dict[str, Any]:
        return {
            "uid": uid,
            "alias": "Living Room Shade",
            "address": "1.1.1",
            "type": "IMC100",
            "form": "1,I,MOTOR,MONO,FALSE",
        }

    def test_parses_light_device(self) -> None:
        devices = _parse_devices([self._light_raw()])
        assert len(devices) == 1
        dev = devices[0]
        assert dev["platform"] == "light"
        assert dev["color_space"] == "HSV"
        assert dev["cct_support"] is True
        assert dev["uid"] == "101"
        assert dev["alias"] == "Office Lights"

    def test_parses_motor_single_channel(self) -> None:
        devices = _parse_devices([self._motor_raw()])
        assert len(devices) == 1
        dev = devices[0]
        assert dev["platform"] == "cover"
        assert dev["uid"] == "201"

    def test_parses_motor_multi_channel(self) -> None:
        raw: dict[str, Any] = {
            "uid": "301",
            "type": "IMC300",
            "form": "2,I,MOTOR,MONO,FALSE",
            "channel_addresses": {"A": "1.1.1", "B": "1.1.2"},
            "channel_aliases": {"A": "Shade A", "B": "Shade B"},
        }
        devices = _parse_devices([raw])
        assert len(devices) == 2
        uids = {d["uid"] for d in devices}
        assert "301_A" in uids
        assert "301_B" in uids
        parent_uids = {d.get("parent_uid") for d in devices}
        assert "301" in parent_uids

    def test_skips_unknown_device_class(self) -> None:
        raw: dict[str, Any] = {
            "uid": "999",
            "form": "1,I,KEYPAD,MONO,FALSE",
        }
        devices = _parse_devices([raw])
        assert devices == []

    def test_mixed_devices(self) -> None:
        devices = _parse_devices([self._light_raw(), self._motor_raw()])
        platforms = {d["platform"] for d in devices}
        assert "light" in platforms
        assert "cover" in platforms


# ---------------------------------------------------------------------------
# ENodeCoordinator.handle_message
# ---------------------------------------------------------------------------


def _make_coordinator() -> ENodeCoordinator:
    mock_client = MagicMock()
    mock_client.is_connected = True
    return ENodeCoordinator(
        hass=MagicMock(),
        client=mock_client,
        devices=[],
        scan_interval=30,
    )


class TestHandleMessage:
    def test_led_value_rgb(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!2.1.1.LED.VALUE=120.80.60")
        state = coord.get_state("2.1.1")
        assert state["r"] == 120
        assert state["g"] == 80
        assert state["b"] == 60
        assert state["is_on"] is True

    def test_led_value_off(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!2.1.1.LED.VALUE=0.0.0")
        assert coord.get_state("2.1.1")["is_on"] is False

    def test_led_value_monochrome(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!2.1.2.LED.VALUE=200")
        state = coord.get_state("2.1.2")
        assert state["brightness_raw"] == 200
        assert state["is_on"] is True

    def test_led_value_rgbw(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!2.1.3.LED.VALUE=100.100.100.50")
        state = coord.get_state("2.1.3")
        assert state["w"] == 50

    def test_led_color_hsv(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!2.1.4.LED.COLOR=120.200.180")
        state = coord.get_state("2.1.4")
        assert state["h"] == 120
        assert state["s"] == 200
        assert state["v"] == 180
        assert state["is_on"] is True

    def test_led_color_off(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!2.1.4.LED.COLOR=0.0.0")
        assert coord.get_state("2.1.4")["is_on"] is False

    def test_led_status_cct(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!2.1.5.LED.STATUS=180,4500")
        state = coord.get_state("2.1.5")
        assert state["sun"] == 180
        assert state["cct"] == 4500

    def test_motor_position(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!1.1.1.MOTOR.POSITION=75.00")
        assert coord.get_state("1.1.1")["position"] == 75.0

    def test_motor_status(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!1.1.2.MOTOR.STATUS=EXTENDING")
        assert coord.get_state("1.1.2")["motor_status"] == "EXTENDING"

    def test_ignores_negative_responses(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("*2.1.1.LED=BADCMD")
        assert coord.get_state("2.1.1") == {}

    def test_ignores_non_prefixed_lines(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("some random text")
        # no state set, no exception
        assert coord._state == {}

    def test_multiple_addresses_independent(self) -> None:
        coord = _make_coordinator()
        coord.handle_message("!2.1.1.LED.VALUE=200")
        coord.handle_message("!2.1.2.LED.VALUE=100")
        assert coord.get_state("2.1.1")["brightness_raw"] == 200
        assert coord.get_state("2.1.2")["brightness_raw"] == 100

    def test_async_set_updated_data_called(self) -> None:
        coord = _make_coordinator()
        updates: list[Any] = []
        coord.async_set_updated_data = updates.append  # type: ignore[method-assign]
        coord.handle_message("!2.1.1.LED.VALUE=150")
        assert len(updates) == 1
