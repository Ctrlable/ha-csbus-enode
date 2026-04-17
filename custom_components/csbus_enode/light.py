"""
Light platform for Converging Systems CS-Bus ILC controllers.

Supports:
  - Monochrome dimming (ILC-100m)
  - Tunable white / CCT (ILC-200E, ILC-400BE)
  - Full-color RGB / RGBW / HSV (ILC-100C, ILC-300, ILC-400)
  - Circadian / SUN control
  - 24-preset recall
  - Effects (sequence, flame, color cycle, random)
  - Dissolve (transition) rate control
"""

from __future__ import annotations

import logging
from typing import Any, cast

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ENodeCoordinator
from .const import (
    CS_DEVICE_LED,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_DEVICES,
    DATA_LIGHT_ENTITIES,
    DOMAIN,
    PLATFORM_LIGHT,
)
from .enode_client import ENodeClient

_LOGGER = logging.getLogger(__name__)

# CS-Bus color range is 0-240 (not 0-255)
CS_MAX = 240
HA_MAX = 255

EFFECTS = [
    "Preset Sequence",
    "Flame",
    "Color Cycle",
    "Random Color",
]

_EFFECT_MAP = {
    "Preset Sequence": "EFFECT,1",
    "Flame": "EFFECT,2",
    "Color Cycle": "EFFECT,3",
    "Random Color": "EFFECT,4",
}


def _cs_to_ha(val: int) -> int:
    """Scale 0-240 → 0-255."""
    return round(val * HA_MAX / CS_MAX)


def _ha_to_cs(val: int) -> int:
    """Scale 0-255 → 0-240."""
    return round(val * CS_MAX / HA_MAX)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ENodeCoordinator = data[DATA_COORDINATOR]
    client: ENodeClient = data[DATA_CLIENT]
    devices: list[dict[str, Any]] = data[DATA_DEVICES]

    lights = [
        CSBusLight(coordinator, client, dev)
        for dev in devices
        if dev["platform"] == PLATFORM_LIGHT
    ]
    async_add_entities(lights)
    hass.data[DOMAIN][entry.entry_id][DATA_LIGHT_ENTITIES] = lights


class CSBusLight(CoordinatorEntity, LightEntity):  # type: ignore[misc]
    """Represents a CS-Bus ILC lighting controller."""

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
        self._color_space = device.get("color_space", "MONO")
        self._cct_support = device.get("cct_support", False)
        self._cct_warm = device.get("cct_warm", 2700)
        self._cct_cool = device.get("cct_cool", 6500)
        self._attr_unique_id = f"csbus_{device['uid']}"
        self._attr_name = device["alias"]
        self._attr_effect_list = EFFECTS
        self._attr_should_poll = False

    # ------------------------------------------------------------------
    # Device info (groups all entities under one device card)
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device["uid"])},
            name=self._device["alias"],
            manufacturer="Converging Systems",
            model=self._device.get("type_name", "ILC"),
        )

    # ------------------------------------------------------------------
    # Color mode
    # ------------------------------------------------------------------

    @property
    def color_mode(self) -> ColorMode:
        if self._color_space == "HSV":
            if self._cct_support:
                return ColorMode.COLOR_TEMP  # full color + CCT
            return ColorMode.HS
        if self._cct_support:
            return ColorMode.COLOR_TEMP
        return ColorMode.BRIGHTNESS

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        modes: set[ColorMode] = set()
        if self._color_space == "HSV":
            modes.add(ColorMode.HS)
        if self._cct_support:
            modes.add(ColorMode.COLOR_TEMP)
        if not modes:
            modes.add(ColorMode.BRIGHTNESS)
        return modes

    @property
    def supported_features(self) -> LightEntityFeature:
        return LightEntityFeature.TRANSITION | LightEntityFeature.EFFECT

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.get_state(self._address)
        return cast("bool | None", state.get("is_on"))

    @property
    def brightness(self) -> int | None:
        state = self.coordinator.get_state(self._address)
        raw: int | None = cast("int | None", state.get("v") or state.get("brightness_raw"))
        if raw is not None:
            return _cs_to_ha(raw)
        return None

    @property
    def hs_color(self) -> tuple[float, float] | None:
        state = self.coordinator.get_state(self._address)
        h = state.get("h")
        s = state.get("s")
        if h is not None and s is not None:
            # CS-Bus hue: 0-240 maps to 0-360 degrees
            hue_deg = round(h * 360 / CS_MAX)
            sat_pct = round(s * 100 / CS_MAX)
            return (float(hue_deg), float(sat_pct))
        return None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        state = self.coordinator.get_state(self._address)
        r = state.get("r")
        g = state.get("g")
        b = state.get("b")
        if r is not None and g is not None and b is not None:
            return (_cs_to_ha(r), _cs_to_ha(g), _cs_to_ha(b))
        return None

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        state = self.coordinator.get_state(self._address)
        r, g, b, w = state.get("r"), state.get("g"), state.get("b"), state.get("w")
        if all(x is not None for x in (r, g, b, w)):
            return (_cs_to_ha(r), _cs_to_ha(g), _cs_to_ha(b), _cs_to_ha(w))
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        state = self.coordinator.get_state(self._address)
        return cast("int | None", state.get("cct"))

    @property
    def min_color_temp_kelvin(self) -> int:
        return cast(int, self._cct_warm)

    @property
    def max_color_temp_kelvin(self) -> int:
        return cast(int, self._cct_cool)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        transition = kwargs.get(ATTR_TRANSITION)
        ramp = f":{int(transition)}" if transition is not None else ""

        if ATTR_EFFECT in kwargs:
            effect_key = kwargs[ATTR_EFFECT]
            cmd = _EFFECT_MAP.get(effect_key)
            if cmd:
                await self._client.async_send_command(self._address, CS_DEVICE_LED, cmd)
                return

        if ATTR_HS_COLOR in kwargs:
            hs = kwargs[ATTR_HS_COLOR]
            brightness = kwargs.get(ATTR_BRIGHTNESS, 240)
            h_cs = round(hs[0] * CS_MAX / 360)
            s_cs = round(hs[1] * CS_MAX / 100)
            v_cs = _ha_to_cs(brightness)
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"HSV,{h_cs}.{s_cs}.{v_cs}{ramp}"
            )
            return

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            r_cs = _ha_to_cs(r)
            g_cs = _ha_to_cs(g)
            b_cs = _ha_to_cs(b)
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"RGB,{r_cs}.{g_cs}.{b_cs}{ramp}"
            )
            return

        if ATTR_RGBW_COLOR in kwargs:
            r, g, b, w = kwargs[ATTR_RGBW_COLOR]
            r_cs = _ha_to_cs(r)
            g_cs = _ha_to_cs(g)
            b_cs = _ha_to_cs(b)
            w_cs = _ha_to_cs(w)
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"RGBW,{r_cs}.{g_cs}.{b_cs}.{w_cs}{ramp}"
            )
            return

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            cct = kwargs[ATTR_COLOR_TEMP_KELVIN]
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"CCT,{cct}{ramp}"
            )
            return

        if ATTR_BRIGHTNESS in kwargs:
            level = _ha_to_cs(kwargs[ATTR_BRIGHTNESS])
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"SET,{level}{ramp}"
            )
            return

        # Plain ON with optional ramp
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"ON{ramp}"
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        transition = kwargs.get(ATTR_TRANSITION)
        ramp = f":{int(transition)}" if transition is not None else ""
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"OFF{ramp}"
        )

    # ------------------------------------------------------------------
    # Extra service calls exposed via HA actions
    # ------------------------------------------------------------------

    async def async_recall_preset(self, preset: int, transition: int | None = None) -> None:
        """Recall a stored preset (1-24)."""
        ramp = f":{transition}" if transition is not None else ""
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"RECALL,{preset}{ramp}"
        )

    async def async_store_preset(self, preset: int) -> None:
        """Store current state to preset (1-24)."""
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"STORE,{preset}"
        )

    async def async_set_circadian(self, level: int, transition: int | None = None) -> None:
        """Set circadian/SUN level (0-240, 0=night, 240=noon)."""
        ramp = f":{transition}" if transition is not None else ""
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"SUN,{level}{ramp}"
        )

    async def async_resume_circadian(self, max_level: int = 240) -> None:
        """Resume an interrupted circadian sequence."""
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"SOLAR,{max_level}"
        )

    async def async_set_dissolve(self, dissolve_index: int, seconds: float) -> None:
        """Set dissolve rate. Index 0=all, 1=direct, 2=on/off+presets, 3=effects."""
        await self._client.async_send_item_command(
            self._address, CS_DEVICE_LED, f"DISSOLVE.{dissolve_index}", str(seconds)
        )
