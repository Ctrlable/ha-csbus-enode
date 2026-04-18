"""
Pytest configuration: mock all homeassistant imports so tests run without
installing the full HA package.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Minimal HA class stubs
# ---------------------------------------------------------------------------


class _DataUpdateCoordinator:
    def __init__(
        self,
        hass: Any,
        logger: Any,
        *,
        name: str,
        update_interval: timedelta,
    ) -> None:
        self.hass = hass
        self._logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data: dict[str, Any] = {}

    def async_set_updated_data(self, data: dict[str, Any]) -> None:
        self.data = data

    async def async_refresh(self) -> None:
        pass


class _UpdateFailed(Exception):
    pass


class _CoordinatorEntity:
    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.entity_id: str = ""


# ---------------------------------------------------------------------------
# Build module stubs and inject into sys.modules BEFORE any integration import
# ---------------------------------------------------------------------------


def _make_module(name: str, **attrs: Any) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _install_ha_stubs() -> None:
    if "homeassistant" in sys.modules:
        return  # already patched

    huc = _make_module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_DataUpdateCoordinator,
        UpdateFailed=_UpdateFailed,
        CoordinatorEntity=_CoordinatorEntity,
    )

    # Sensor stubs
    class _SensorStateClass:
        MEASUREMENT = "measurement"

    sensor_mod = _make_module(
        "homeassistant.components.sensor",
        SensorEntity=object,
        SensorStateClass=_SensorStateClass,
    )

    # EntityCategory stub
    class _EntityCategory:
        DIAGNOSTIC = "diagnostic"

    class _Platform:
        LIGHT = "light"
        COVER = "cover"
        SENSOR = "sensor"

    const_mod = _make_module(
        "homeassistant.const",
        CONF_HOST="host",
        ATTR_ENTITY_ID="entity_id",
        Platform=_Platform,
        EntityCategory=_EntityCategory,
    )

    # DeviceInfo stub
    class _DeviceInfo(dict):  # type: ignore[misc]
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)

    entity_mod = _make_module("homeassistant.helpers.entity", DeviceInfo=_DeviceInfo)
    entity_platform_mod = _make_module(
        "homeassistant.helpers.entity_platform",
        AddEntitiesCallback=Any,
    )
    aiohttp_client_mod = _make_module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=MagicMock(),
    )

    # Light stubs
    class _LightEntityFeature:
        TRANSITION = 1
        EFFECT = 4

    class _ColorMode:
        BRIGHTNESS = "brightness"
        HS = "hs"
        COLOR_TEMP = "color_temp"

    light_mod = _make_module(
        "homeassistant.components.light",
        LightEntity=object,
        LightEntityFeature=_LightEntityFeature,
        ColorMode=_ColorMode,
        ATTR_BRIGHTNESS="brightness",
        ATTR_COLOR_TEMP_KELVIN="color_temp_kelvin",
        ATTR_EFFECT="effect",
        ATTR_HS_COLOR="hs_color",
        ATTR_RGB_COLOR="rgb_color",
        ATTR_RGBW_COLOR="rgbw_color",
        ATTR_TRANSITION="transition",
    )

    # Cover stubs
    class _CoverEntityFeature:
        OPEN = 1
        CLOSE = 2
        STOP = 4
        SET_POSITION = 8

    class _CoverDeviceClass:
        SHADE = "shade"

    cover_mod = _make_module(
        "homeassistant.components.cover",
        CoverEntity=object,
        CoverEntityFeature=_CoverEntityFeature,
        CoverDeviceClass=_CoverDeviceClass,
        ATTR_POSITION="position",
    )

    config_entries_mod = _make_module("homeassistant.config_entries", ConfigEntry=MagicMock)
    core_mod = _make_module(
        "homeassistant.core",
        HomeAssistant=MagicMock,
        ServiceCall=MagicMock,
        callback=lambda f: f,
    )
    cv_mod = _make_module(
        "homeassistant.helpers.config_validation",
        entity_ids=MagicMock(),
    )

    stubs: dict[str, ModuleType] = {
        "homeassistant": _make_module("homeassistant"),
        "homeassistant.const": const_mod,
        "homeassistant.core": core_mod,
        "homeassistant.config_entries": config_entries_mod,
        "homeassistant.helpers": _make_module("homeassistant.helpers"),
        "homeassistant.helpers.event": _make_module(
            "homeassistant.helpers.event",
            async_track_time_interval=MagicMock(),
        ),
        "homeassistant.helpers.update_coordinator": huc,
        "homeassistant.helpers.entity": entity_mod,
        "homeassistant.helpers.entity_platform": entity_platform_mod,
        "homeassistant.helpers.aiohttp_client": aiohttp_client_mod,
        "homeassistant.helpers.config_validation": cv_mod,
        "homeassistant.components": _make_module("homeassistant.components"),
        "homeassistant.components.light": light_mod,
        "homeassistant.components.cover": cover_mod,
        "homeassistant.components.sensor": sensor_mod,
        "voluptuous": MagicMock(),
    }

    sys.modules.update(stubs)


_install_ha_stubs()
