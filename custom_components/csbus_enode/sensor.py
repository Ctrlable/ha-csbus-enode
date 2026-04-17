"""
Diagnostic sensor platform for the Converging Systems e-Node CS-Bus integration.

Exposes hub-level information:
  - Connection status (connected / disconnected)
  - Firmware version (fetched from the e-Node web interface on setup)
  - Device count (number of discovered CS-Bus devices)
"""

from __future__ import annotations

import logging
from typing import cast

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ENodeCoordinator
from .const import (
    CONF_HOST,
    DATA_COORDINATOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ENodeCoordinator = data[DATA_COORDINATOR]

    # Fetch firmware version once at setup time
    if coordinator.firmware_version is None:
        session = async_get_clientsession(hass)
        coordinator.firmware_version = await coordinator.client.async_fetch_firmware_version(
            session
        )

    async_add_entities(
        [
            CSBusConnectionSensor(coordinator, entry),
            CSBusFirmwareSensor(coordinator, entry),
            CSBusDeviceCountSensor(coordinator, entry),
        ]
    )


# ---------------------------------------------------------------------------
# Base helper
# ---------------------------------------------------------------------------


class _CSBusDiagnosticSensor(CoordinatorEntity, SensorEntity):  # type: ignore[misc]  # noqa: SIM105
    """Base class for e-Node diagnostic sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, coordinator: ENodeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._host: str = entry.data[CONF_HOST]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"hub_{self._entry.entry_id}")},
            name=f"e-Node ({self._host})",
            manufacturer="Converging Systems",
            model="e-Node",
        )


# ---------------------------------------------------------------------------
# Concrete sensor entities
# ---------------------------------------------------------------------------


class CSBusConnectionSensor(_CSBusDiagnosticSensor):
    """Reports whether the Telnet connection to the e-Node is active."""

    _attr_icon = "mdi:lan-connect"

    def __init__(self, coordinator: ENodeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"csbus_hub_{entry.entry_id}_connection"
        self._attr_name = "Connection"

    @property
    def native_value(self) -> str:
        return "connected" if self.coordinator.client.is_connected else "disconnected"


class CSBusFirmwareSensor(_CSBusDiagnosticSensor):
    """Reports the e-Node firmware version string."""

    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: ENodeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"csbus_hub_{entry.entry_id}_firmware"
        self._attr_name = "Firmware Version"

    @property
    def native_value(self) -> str | None:
        return cast("str | None", self.coordinator.firmware_version)


class CSBusDeviceCountSensor(_CSBusDiagnosticSensor):
    """Reports the number of CS-Bus devices discovered on this e-Node."""

    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ENodeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"csbus_hub_{entry.entry_id}_device_count"
        self._attr_name = "Device Count"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.devices)
