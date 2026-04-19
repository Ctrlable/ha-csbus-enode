"""
Light platform for Converging Systems CS-Bus / DMX / DALI controllers.

BUG FIXES IN THIS VERSION
--------------------------

FIX 1 — Dim from OFF doesn't turn on the fixture
  Root cause: CS-Bus SET command adjusts brightness on an ALREADY-ON fixture.
  If the fixture is OFF, SET is silently ignored.
  Fix: When is_on is False or unknown, send ON first, then SET.
  Better: Use SET,level which the DDK says works as a brightness command
  while the light is on — so we always send ON then SET in sequence.
  For plain brightness-only from off: send ON:0 (instant on) then SET,level.

FIX 2 — CCT slider changes level bar color but fixture ignores temperature
  Root cause 1: color_mode returns HS when cct state is None (no NOTIFY).
    When color_mode=HS, HA does NOT send ATTR_COLOR_TEMP_KELVIN in kwargs —
    it sends nothing useful when you move the CCT slider.
  Root cause 2: CCT command doesn't turn on a fixture that is off.
  Fix: Track whether the user's LAST command was color or CCT using an
    explicit _last_mode flag. Use this to drive color_mode consistently.
    Always send ON before CCT if the fixture is off.

FIX 3 — Color bar (brightness slider gradient) doesn't match selected color
  Root cause: HA's brightness slider only shows colour gradient when
    color_mode == ColorMode.HS AND hs_color returns a valid (h, s) tuple.
    After a color command, we store h/s in _opt_state correctly, but HA
    also requires color_mode to be HS at that exact moment.
    When color_mode dynamically switches based on state.get("cct"), and
    cct is None by default, it returns HS — but hs_color returns None
    because no h/s are in state yet on first load.
  Fix: Seed _opt_state with a default h/s for HSV devices on init.
    Also: after any CCT command, explicitly clear h/s from opt_state so
    hs_color returns None and color_mode cleanly stays COLOR_TEMP.
    After any color command, explicitly set cct=None in opt_state.

COLOR MODE RULES (Home Assistant specification)
------------------------------------------------
  BRIGHTNESS mode:  light has no color, only dim level
  COLOR_TEMP mode:  light has tunable white only (no hue/sat)
  HS mode:          light has hue+saturation colour
  
  HA rules:
  - supported_color_modes declares ALL modes the device supports
  - color_mode returns the CURRENTLY ACTIVE mode
  - When color_mode=HS, hs_color MUST return a valid tuple
  - When color_mode=COLOR_TEMP, color_temp_kelvin MUST return a value
  - When color_mode=BRIGHTNESS, neither hs_color nor color_temp_kelvin
    should return values

CCT KELVIN RANGE CONVENTION
-----------------------------
  HA min_color_temp_kelvin = coolest/coldest (HIGHEST Kelvin, e.g. 6500)
  HA max_color_temp_kelvin = warmest (LOWEST Kelvin, e.g. 2700)
  This is the OPPOSITE of intuition — hotter colour = higher Kelvin = min.

OPTIMISTIC STATE
-----------------
  DMX fixtures ship with NOTIFY=OFF — no feedback is sent after commands.
  We maintain _opt_state which overrides coordinator state, updated
  immediately after every command so the UI stays in sync.
"""

from __future__ import annotations

import logging
from typing import Any

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

from .const import CS_DEVICE_LED, DATA_CLIENT, DATA_COORDINATOR, DATA_DEVICES, DOMAIN, PLATFORM_LIGHT
from .enode_client import ENodeClient
from . import ENodeCoordinator

_LOGGER = logging.getLogger(__name__)

# CS-Bus range: 0-240.  HA brightness range: 0-255.
CS_MAX = 240
HA_MAX = 255

EFFECTS = ["Preset Sequence", "Flame", "Color Cycle", "Random Color"]
_EFFECT_MAP = {
    "Preset Sequence": "EFFECT,1",
    "Flame":           "EFFECT,2",
    "Color Cycle":     "EFFECT,3",
    "Random Color":    "EFFECT,4",
}

# Sentinel values for _last_mode
_MODE_COLOR = "color"
_MODE_CCT   = "cct"
_MODE_DIM   = "dim"


def _cs_to_ha(val: int) -> int:
    """Scale CS-Bus 0-240 → HA 0-255, clamped."""
    return min(255, max(0, round(int(val) * HA_MAX / CS_MAX)))


def _ha_to_cs(val: int) -> int:
    """Scale HA 0-255 → CS-Bus 0-240, clamped."""
    return min(240, max(0, round(int(val) * CS_MAX / HA_MAX)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data        = hass.data[DOMAIN][entry.entry_id]
    coordinator = data[DATA_COORDINATOR]
    client      = data[DATA_CLIENT]
    devices     = data[DATA_DEVICES]

    lights = [
        CSBusLight(coordinator, client, dev)
        for dev in devices
        if dev["platform"] == PLATFORM_LIGHT
    ]
    _LOGGER.debug("Setting up %d light(s) for %s", len(lights), entry.data.get("host"))
    async_add_entities(lights)


class CSBusLight(CoordinatorEntity, LightEntity):
    """
    One CS-Bus / DMX / DALI light controller channel.

    Optimistic: updates local state immediately after every command
    so the HA UI responds instantly even when NOTIFY=OFF.
    """

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ENodeCoordinator,
        client: ENodeClient,
        device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._client      = client
        self._device      = device
        self._address     = device["address"]
        self._color_space = device.get("color_space", "MONO")
        self._cct_support = device.get("cct_support", False)

        # HA convention: min = coldest (high K), max = warmest (low K)
        self._cct_cold_k  = device.get("cct_cool", 6500)   # e.g. 6500 K
        self._cct_warm_k  = device.get("cct_warm", 2700)   # e.g. 2700 K

        self._attr_unique_id   = f"csbus_{device['uid']}"
        self._attr_name        = device["alias"]
        self._attr_effect_list = EFFECTS

        # Optimistic state — overrides coordinator state after each command
        # Seed with a mid-point colour so the gradient bar shows from first use
        if self._color_space == "HSV":
            # Default: neutral white hue (hue=0, sat=0 = white in HSV)
            self._opt_state: dict[str, Any] = {
                "h": 0, "s": 0, "v": CS_MAX // 2,
                "is_on": None,
            }
        else:
            self._opt_state = {"brightness_raw": CS_MAX // 2, "is_on": None}

        # Track last command type so color_mode stays consistent
        # without depending on NOTIFY feedback
        self._last_mode: str = _MODE_CCT if self._cct_support and self._color_space != "HSV" else _MODE_DIM

        # Previous coordinator state snapshot — used in _handle_coordinator_update
        # to detect which kind of state change arrived (CCT / color / brightness).
        self._prev_coord_state: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Coordinator update hook
    # ------------------------------------------------------------------

    def _handle_coordinator_update(self) -> None:
        """
        Called whenever the coordinator fires a new state update.

        Two jobs:
        1. Update _last_mode when an EXTERNAL command (keypad, Lutron, web UI)
           changed the fixture into CCT or color mode.  Without this, HA keeps
           showing the wrong color_mode after external commands.

        2. Evict opt_state keys that are now confirmed by real device state.
           opt_state was written optimistically after OUR last command; once the
           coordinator receives the echo (or a poll result) for the same key,
           the real value should take over so external changes show through.
           Exception: None sentinels in opt_state mean "actively suppress this
           coordinator key" (e.g. h/s cleared after a CCT command) — keep those.
        """
        coord = self.coordinator.get_state(self._address)
        prev  = self._prev_coord_state

        # --- 1. Update _last_mode from coordinator state delta ---
        cct_arrived = coord.get("cct") is not None and coord.get("cct") != prev.get("cct")
        color_arrived = (
            coord.get("h") is not None
            and (coord.get("h") != prev.get("h") or coord.get("s") != prev.get("s"))
        )
        if cct_arrived:
            self._last_mode = _MODE_CCT
        elif color_arrived:
            self._last_mode = _MODE_COLOR

        self._prev_coord_state = dict(coord)

        # --- 2. Evict stale opt_state overrides ---
        for key in list(self._opt_state):
            if self._opt_state[key] is not None and coord.get(key) is not None:
                del self._opt_state[key]

        super()._handle_coordinator_update()

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device["uid"])},
            name=self._device["alias"],
            manufacturer="Converging Systems",
            model=self._device.get("type_name", "ILC"),
            configuration_url=f"http://{self._client.host}",
        )

    # ------------------------------------------------------------------
    # Color mode
    # ------------------------------------------------------------------

    @property
    def color_mode(self) -> ColorMode:
        """
        Return the CURRENTLY ACTIVE color mode.

        Rules:
        - Pure dimmer (MONO, no CCT)     → always BRIGHTNESS
        - Tunable white (MONO + CCT)     → always COLOR_TEMP
        - Full color, no CCT             → always HS
        - Full color + CCT               → HS after color command,
                                           COLOR_TEMP after CCT command
                                           (tracked via _last_mode)
        """
        if self._color_space != "HSV":
            return ColorMode.COLOR_TEMP if self._cct_support else ColorMode.BRIGHTNESS

        # HSV device
        if not self._cct_support:
            return ColorMode.HS

        # HSV + CCT: use last-command tracking
        return ColorMode.COLOR_TEMP if self._last_mode == _MODE_CCT else ColorMode.HS

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
    # State — merge coordinator (NOTIFY) + optimistic overrides
    # ------------------------------------------------------------------

    def _merged_state(self) -> dict[str, Any]:
        base = dict(self.coordinator.get_state(self._address))
        # Optimistic values win over coordinator for keys we explicitly set
        for k, v in self._opt_state.items():
            if v is not None:
                base[k] = v
            elif k in base:
                # explicit None in opt_state means "clear this key"
                del base[k]
        return base

    @property
    def is_on(self) -> bool | None:
        val = self._merged_state().get("is_on")
        return bool(val) if val is not None else None

    @property
    def brightness(self) -> int | None:
        state = self._merged_state()
        # v = HSV value component, brightness_raw = mono brightness
        raw = state.get("v") if state.get("v") is not None else state.get("brightness_raw")
        return _cs_to_ha(int(raw)) if raw is not None else None

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """
        Return (hue_degrees, saturation_percent) or None.
        MUST return None when color_mode != HS to avoid HA confusion.
        """
        if self.color_mode != ColorMode.HS:
            return None
        state = self._merged_state()
        h = state.get("h")
        s = state.get("s")
        if h is not None and s is not None:
            return (round(int(h) * 360 / CS_MAX, 1), round(int(s) * 100 / CS_MAX, 1))
        return None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if self.color_mode != ColorMode.HS:
            return None
        state = self._merged_state()
        r, g, b = state.get("r"), state.get("g"), state.get("b")
        if r is not None and g is not None and b is not None:
            return (_cs_to_ha(int(r)), _cs_to_ha(int(g)), _cs_to_ha(int(b)))
        return None

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        if self.color_mode != ColorMode.HS:
            return None
        state = self._merged_state()
        r, g, b, w = state.get("r"), state.get("g"), state.get("b"), state.get("w")
        if all(x is not None for x in (r, g, b, w)):
            return (_cs_to_ha(int(r)), _cs_to_ha(int(g)), _cs_to_ha(int(b)), _cs_to_ha(int(w)))
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        """
        Return CCT in Kelvin or None.
        MUST return None when color_mode != COLOR_TEMP.
        """
        if self.color_mode != ColorMode.COLOR_TEMP:
            return None
        state = self._merged_state()
        cct = state.get("cct")
        if cct is not None:
            return int(cct)
        # No NOTIFY feedback — return midpoint so the slider is not blank
        mid = (self._cct_cold_k + self._cct_warm_k) // 2
        return mid

    @property
    def min_color_temp_kelvin(self) -> int:
        return self._cct_cold_k   # coldest = highest K = HA "min"

    @property
    def max_color_temp_kelvin(self) -> int:
        return self._cct_warm_k   # warmest = lowest K = HA "max"

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        """
        Turn on or adjust the light.

        KEY BEHAVIOURAL RULES:
        1. If the light is currently OFF (or state unknown), always send ON
           before any SET/CCT/color command — most fixtures ignore those
           commands when they are off.
        2. SET,level adjusts brightness on a RUNNING fixture. It does NOT
           turn the fixture on. Always pair with ON when off.
        3. CCT,K sets colour temperature. Send ON first if off.
        4. HSV,h.s.v is a combined colour+brightness command that also
           turns the fixture on — no separate ON needed.
        """
        transition = kwargs.get(ATTR_TRANSITION)
        ramp = f":{int(transition)}" if transition is not None else ""
        currently_on = self.is_on  # may be None if state unknown

        # --- Effect ---
        if ATTR_EFFECT in kwargs:
            cmd = _EFFECT_MAP.get(kwargs[ATTR_EFFECT])
            if cmd:
                if not currently_on:
                    await self._client.async_send_command(self._address, CS_DEVICE_LED, "ON")
                await self._client.async_send_command(self._address, CS_DEVICE_LED, cmd)
                self._opt_state["is_on"] = True
                self._async_write_ha_state()
            return

        # --- HS color —
        # HSV command implicitly turns on the fixture (brightness V > 0)
        if ATTR_HS_COLOR in kwargs:
            hs     = kwargs[ATTR_HS_COLOR]
            bri_ha = kwargs.get(ATTR_BRIGHTNESS) or self.brightness or HA_MAX
            h_cs   = round(hs[0] * CS_MAX / 360)
            s_cs   = round(hs[1] * CS_MAX / 100)
            v_cs   = _ha_to_cs(int(bri_ha))
            # Ensure V > 0 so the fixture actually turns on
            v_cs   = max(1, v_cs)
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"HSV,{h_cs}.{s_cs}.{v_cs}{ramp}"
            )
            self._opt_state.update(h=h_cs, s=s_cs, v=v_cs, is_on=True)
            # Clear CCT so color_mode switches to HS
            self._opt_state["cct"] = None
            self._last_mode = _MODE_COLOR
            self._async_write_ha_state()
            return

        # --- RGB ---
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            r_cs, g_cs, b_cs = _ha_to_cs(r), _ha_to_cs(g), _ha_to_cs(b)
            if not currently_on:
                await self._client.async_send_command(self._address, CS_DEVICE_LED, "ON")
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"RGB,{r_cs}.{g_cs}.{b_cs}{ramp}"
            )
            self._opt_state.update(r=r_cs, g=g_cs, b=b_cs, is_on=True)
            self._opt_state["cct"] = None
            self._last_mode = _MODE_COLOR
            self._async_write_ha_state()
            return

        # --- RGBW ---
        if ATTR_RGBW_COLOR in kwargs:
            r, g, b, w = kwargs[ATTR_RGBW_COLOR]
            r_cs, g_cs, b_cs, w_cs = _ha_to_cs(r), _ha_to_cs(g), _ha_to_cs(b), _ha_to_cs(w)
            if not currently_on:
                await self._client.async_send_command(self._address, CS_DEVICE_LED, "ON")
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"RGBW,{r_cs}.{g_cs}.{b_cs}.{w_cs}{ramp}"
            )
            self._opt_state.update(r=r_cs, g=g_cs, b=b_cs, w=w_cs, is_on=True)
            self._opt_state["cct"] = None
            self._last_mode = _MODE_COLOR
            self._async_write_ha_state()
            return

        # --- Color temperature ---
        # CCT does NOT turn on a dark fixture — must send ON first
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            cct_k = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
            if not currently_on:
                await self._client.async_send_command(self._address, CS_DEVICE_LED, "ON")
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"CCT,{cct_k}{ramp}"
            )
            # Store CCT and clear h/s so color_mode returns COLOR_TEMP
            self._opt_state["cct"] = cct_k
            self._opt_state["h"]   = None
            self._opt_state["s"]   = None
            self._opt_state["is_on"] = True
            self._last_mode = _MODE_CCT
            # Handle optional brightness together with CCT
            if ATTR_BRIGHTNESS in kwargs:
                level = max(1, _ha_to_cs(int(kwargs[ATTR_BRIGHTNESS])))
                await self._client.async_send_command(
                    self._address, CS_DEVICE_LED, f"SET,{level}{ramp}"
                )
                self._opt_state["brightness_raw"] = level
                self._opt_state["v"] = level
            self._async_write_ha_state()
            return

        # --- Brightness only ---
        # SET adjusts level on a running fixture. If off, send ON first.
        # Using ramp-to-level: ON turns on, then SET sets the exact level.
        if ATTR_BRIGHTNESS in kwargs:
            level = max(1, _ha_to_cs(int(kwargs[ATTR_BRIGHTNESS])))
            if not currently_on:
                # Turn on first — without this, SET is silently ignored
                await self._client.async_send_command(self._address, CS_DEVICE_LED, "ON")
            await self._client.async_send_command(
                self._address, CS_DEVICE_LED, f"SET,{level}{ramp}"
            )
            self._opt_state.update(
                v=level, brightness_raw=level,
                is_on=True,
            )
            self._last_mode = _MODE_DIM
            self._async_write_ha_state()
            return

        # --- Plain ON ---
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"ON{ramp}"
        )
        self._opt_state["is_on"] = True
        self._async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        transition = kwargs.get(ATTR_TRANSITION)
        ramp = f":{int(transition)}" if transition is not None else ""
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"OFF{ramp}"
        )
        self._opt_state["is_on"] = False
        self._async_write_ha_state()

    # ------------------------------------------------------------------
    # Extra service handlers
    # ------------------------------------------------------------------

    async def async_recall_preset(self, preset: int, transition: int | None = None) -> None:
        """Recall a stored preset (1-24)."""
        ramp = f":{transition}" if transition is not None else ""
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"RECALL,{preset}{ramp}"
        )
        self._opt_state["is_on"] = True
        self._async_write_ha_state()

    async def async_store_preset(self, preset: int) -> None:
        """Store current state to preset (1-24)."""
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"STORE,{preset}"
        )

    async def async_set_circadian(self, level: int, transition: int | None = None) -> None:
        """Set circadian/SUN level (0=night, 240=noon sun)."""
        ramp = f":{transition}" if transition is not None else ""
        if not self.is_on:
            await self._client.async_send_command(self._address, CS_DEVICE_LED, "ON")
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"SUN,{level}{ramp}"
        )
        self._opt_state["is_on"] = True
        self._async_write_ha_state()

    async def async_resume_circadian(self, max_level: int = 240) -> None:
        """Resume an interrupted circadian schedule."""
        await self._client.async_send_command(
            self._address, CS_DEVICE_LED, f"SOLAR,{max_level}"
        )
        self._opt_state["is_on"] = True
        self._async_write_ha_state()

    async def async_set_dissolve(self, dissolve_index: int, seconds: float) -> None:
        """Set dissolve/fade rate (index 0=all, 1=direct, 2=on/off, 3=effects)."""
        await self._client.async_send_item_command(
            self._address, CS_DEVICE_LED, f"DISSOLVE.{dissolve_index}", str(seconds)
        )
