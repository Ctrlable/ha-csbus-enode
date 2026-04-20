"""
Cover platform for Converging Systems CS-Bus IMC motor controllers.

Supports:
  - IMC-100 (single-channel, position feedback)
  - IMC-300 (multi-channel with digital encoding, per-channel aliases)
  - BRIC masking/screen controllers

Position semantics (CS-Bus):
  0.00  = fully retracted / home (UP)
  100.00 = fully deployed / extended (DOWN)
Home Assistant position semantics are inverted:
  0   = fully closed (down)
  100 = fully open (up)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.components.cover import ATTR_CURRENT_POSITION
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CS_DEVICE_MOTOR,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_DEVICES,
    DOMAIN,
    MOTOR_STATUS_EXTENDING,
    MOTOR_STATUS_HOME,
    MOTOR_STATUS_RETRACTING,
    MOTOR_STATUS_STOP,
    PLATFORM_COVER,
)
from .enode_client import ENodeClient
from . import ENodeCoordinator

_LOGGER = logging.getLogger(__name__)


def _cs_pos_to_ha(cs_pos: float) -> int:
    """CS 0=up, 100=down  →  HA 0=closed, 100=open."""
    return round(100.0 - cs_pos)


def _ha_pos_to_cs(ha_pos: int) -> float:
    """HA 0=closed, 100=open  →  CS 0=up, 100=down."""
    return 100.0 - ha_pos


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ENodeCoordinator = data[DATA_COORDINATOR]
    client: ENodeClient = data[DATA_CLIENT]
    devices: list[dict] = data[DATA_DEVICES]

    covers = [
        CSBusCover(coordinator, client, dev)
        for dev in devices
        if dev["platform"] == PLATFORM_COVER
    ]
    async_add_entities(covers)


class CSBusCover(CoordinatorEntity, CoverEntity, RestoreEntity):
    """Represents a CS-Bus IMC motor controller channel."""

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        coordinator: ENodeCoordinator,
        client: ENodeClient,
        device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._device = device
        self._address = device["address"]
        self._attr_unique_id = f"csbus_{device['uid']}"
        self._attr_name = device["alias"]
        self._attr_should_poll = False

    @property
    def device_info(self) -> DeviceInfo:
        parent_uid = self._device.get("parent_uid", self._device["uid"])
        return DeviceInfo(
            identifiers={(DOMAIN, parent_uid)},
            name=self._device["alias"].rsplit(" Ch ", 1)[0] if " Ch " in self._device["alias"] else self._device["alias"],
            manufacturer="Converging Systems",
            model=self._device.get("type_name", "IMC"),
        )

    # ------------------------------------------------------------------
    # State restoration
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if not last_state or last_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        attrs = last_state.attributes
        if ATTR_CURRENT_POSITION in attrs:
            ha_pos = int(attrs[ATTR_CURRENT_POSITION])
            self.coordinator._state.setdefault(self._address, {})["position"] = _ha_pos_to_cs(ha_pos)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def current_cover_position(self) -> int | None:
        state = self.coordinator.get_state(self._address)
        cs_pos = state.get("position")
        if cs_pos is not None:
            return _cs_pos_to_ha(cs_pos)
        return None

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos == 0

    @property
    def is_opening(self) -> bool:
        state = self.coordinator.get_state(self._address)
        return state.get("motor_status") == MOTOR_STATUS_RETRACTING

    @property
    def is_closing(self) -> bool:
        state = self.coordinator.get_state(self._address)
        return state.get("motor_status") == MOTOR_STATUS_EXTENDING

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Retract the shade (move to home/up position)."""
        await self._client.async_send_command(self._address, CS_DEVICE_MOTOR, "RETRACT")

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Extend the shade (move to fully deployed/down position)."""
        await self._client.async_send_command(self._address, CS_DEVICE_MOTOR, "DOWN")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop movement at current position."""
        await self._client.async_send_command(self._address, CS_DEVICE_MOTOR, "STOP")

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the shade to a specific position."""
        ha_pos = kwargs[ATTR_POSITION]
        cs_pos = _ha_pos_to_cs(ha_pos)
        await self._client.async_send_command(
            self._address, CS_DEVICE_MOTOR, f"GOTO,{cs_pos:.0f}"
        )

    async def async_recall_preset(self, preset: int) -> None:
        """Move to a stored preset position (0=home, 1-24=stored)."""
        await self._client.async_send_command(
            self._address, CS_DEVICE_MOTOR, f"RECALL,{preset}"
        )

    async def async_store_preset(self, preset: int) -> None:
        """Store current position as preset (1-24)."""
        await self._client.async_send_command(
            self._address, CS_DEVICE_MOTOR, f"STORE,{preset}"
        )
