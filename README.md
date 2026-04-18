# Converging Systems e-Node CS-Bus — Home Assistant Integration

[![HACS Custom Repository](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2023.3%2B-blue.svg)](https://www.home-assistant.io/)

Integrate your **Converging Systems e-Node** gateway and **CS-Bus lighting / motor controllers** into Home Assistant. Control full-color LEDs, tunable white fixtures, circadian lighting, and motorized shades — all from HA dashboards, automations, and voice assistants.

---

## Supported Hardware

| Device | Type | Notes |
|--------|------|-------|
| **e-Node / e-Node MKIII** | Gateway | Ethernet adapter; required |
| **ILC-100C / ILC-300** | RGB light | Full-color HSV/RGB |
| **ILC-100m** | Mono light | Single-channel dimming |
| **ILC-200E / ILC-400BE** | Tunable white | CCT + brightness |
| **ILC-400 / ILC-450** | Full-color + CCT | HSV + colour temp |
| **IMC-100** | Motor/shade | Single-channel |
| **IMC-300 / IMC-300 MKIII** | Motor/shade | 1–4 channels with position feedback |
| **BRIC Masking Controller** | Motor/shade | Via e-Node translation mode |

---

## Features

- 🔍 **Auto-discovery** — runs `DISCOVER` at startup to find all devices and their names
- 💡 **Full lighting support** — on/off, dimming, RGB, RGBW, HSV, CCT, circadian/SUN
- 🌅 **Circadian lighting** — set level, resume schedule, solar noon control
- 🎬 **Effects** — Preset Sequence, Flame, Color Cycle, Random Color
- 📂 **24 presets** — store and recall lighting scenes or shade positions
- 🪟 **Motor/shade control** — open, close, stop, set position (0–100%)
- ↔️ **Bi-directional feedback** — real-time state via NOTIFY push messages
- 🔄 **Smooth transitions** — inline ramp time on every command (e.g. fade over 30 s)
- ⚙️ **Dissolve rates** — configure per-function transition speeds
- 🔌 **Auto-reconnect** — recovers from network drops transparently

---

## Prerequisites

1. A Converging Systems **e-Node** (any version) connected to your LAN
2. **Telnet must be enabled** on the e-Node:
   - Open e-Node Pilot → expand your e-Node → select **Telnet** → set **Server = Enable** → click **Restart**
3. Note the e-Node's **IP address** (set a static IP for reliability)
4. Your CS-Bus devices must be **commissioned** (UIDs assigned, ZGN addresses set) using e-Node Pilot before adding this integration

---

## Installation

### Via HACS (recommended)

1. In HACS → **Integrations** → ⋮ → **Custom Repositories**
2. Add `https://github.com/ctrlable/ha-csbus-enode` as an **Integration**
3. Search for **Converging Systems e-Node** and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/csbus_enode/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Converging Systems e-Node**
3. Enter:
   - **IP Address** of your e-Node
   - **Telnet Port** (default: `23`)
   - **Username** (default: `Telnet 1`)
   - **Password** (default: `Password 1`)
4. HA will connect, run discovery, and create entities for all found devices

> **Legacy e-Node (pre-MKIII):** Username = `E-NODE`, Password = `ADMIN`

---

## Entities Created

After setup, HA creates:

- **`light.*`** for each ILC lighting controller (named from alias set in e-Node Pilot)
- **`cover.*`** for each IMC motor channel (named from alias set in e-Node Pilot)

---

## Custom Services

Beyond standard HA light/cover services, this integration exposes:

### `csbus_enode.csbus_recall_preset`
Recall a lighting or shade preset (1–24). Preset 0 = home for motors.
```yaml
service: csbus_enode.csbus_recall_preset
target:
  entity_id: light.theater_lights
data:
  preset: 3
  transition: 2   # optional, seconds
```

### `csbus_enode.csbus_store_preset`
Save current state to a preset slot.
```yaml
service: csbus_enode.csbus_store_preset
target:
  entity_id: light.theater_lights
data:
  preset: 3
```

### `csbus_enode.csbus_set_circadian`
Set a circadian lighting level (0 = night, 240 = noon sun).
```yaml
service: csbus_enode.csbus_set_circadian
target:
  entity_id: light.office_lights
data:
  level: 180
  transition: 10
```

### `csbus_enode.csbus_resume_circadian`
Resume an interrupted circadian schedule.
```yaml
service: csbus_enode.csbus_resume_circadian
target:
  entity_id: light.office_lights
data:
  max_level: 240
```

### `csbus_enode.csbus_set_dissolve`
Control per-function fade speed:
- Index 1 = direct value changes (SET, HUE, SAT…)
- Index 2 = ON/OFF and preset transitions
- Index 3 = Effect 1 & 4
- Index 4 = Effect 3 cycle time
- Index 0 = all simultaneously
```yaml
service: csbus_enode.csbus_set_dissolve
target:
  entity_id: light.living_room
data:
  dissolve_index: 2
  seconds: 3
```

---

## Automation Examples

### Morning scene — circadian ramp
```yaml
automation:
  - alias: "Morning Light Ramp"
    trigger:
      platform: time
      at: "07:00:00"
    action:
      - service: csbus_enode.csbus_set_circadian
        target:
          entity_id: light.bedroom_lights
        data:
          level: 100
          transition: 1800  # 30-minute sunrise
```

### Movie mode
```yaml
automation:
  - alias: "Movie Mode"
    trigger:
      platform: state
      entity_id: media_player.living_room_tv
      to: "playing"
    action:
      - service: csbus_enode.csbus_recall_preset
        target:
          entity_id: light.living_room_lights
        data:
          preset: 5
          transition: 3
      - service: cover.close_cover
        target:
          entity_id: cover.living_room_screen
```

### Shade position by sun elevation
```yaml
automation:
  - alias: "Afternoon Sun Protection"
    trigger:
      platform: numeric_state
      entity_id: sun.sun
      attribute: elevation
      above: 40
    action:
      - service: cover.set_cover_position
        target:
          entity_id: cover.south_shades
        data:
          position: 30
```

---

## Addressing Reference

CS-Bus uses **Zone.Group.Node** (Z.G.N) addressing:
- **Zone** = building area (like a floor)
- **Group** = room or zone within the area
- **Node** = specific device

Use `0` as a wildcard: `2.1.0` targets all nodes in zone 2, group 1.

| Default | Device Type |
|---------|------------|
| `2.1.0` | Lighting (factory default) |
| `1.1.0` | Motor (factory default) |

---

## Troubleshooting

**Devices not discovered:**
- Ensure Telnet is enabled on the e-Node (disabled by default)
- Devices must be commissioned with UIDs in e-Node Pilot
- Check firewall — TCP port 23 must be reachable

**States not updating:**
- Enable NOTIFY on your controllers via e-Node Pilot
- The integration falls back to polling (configurable interval) if push fails

**Connection drops:**
- Set a static IP on the e-Node to prevent DHCP address changes
- The integration auto-reconnects; check HA logs if persistent

**Wrong username/password:**
- Legacy e-Node: `E-NODE` / `ADMIN`
- e-Node MKIII+: `Telnet 1` / `Password 1` (or your customized credentials)

**Device Count shows 0 after connecting:**
This was a known bug in v1.0.0 fixed in v1.1.0. Three issues caused it:
- The DISCOVER command was missing its required `>` prefix — the e-Node Telnet shell requires `>DISCOVER` to distinguish shell commands from CS-Bus traffic
- The message parser only split on `;` breaking DISCOVER line-terminated responses (`+UID101\r\n`)
- The 15-second timeout was too short for DALI buses that enumerate fixtures sequentially

Update to v1.1.0 to resolve all three issues.

---

## Contributing

Issues and PRs welcome at [github.com/ctrlable/ha-csbus-enode](https://github.com/ctrlable/ha-csbus-enode).

---

## License

MIT License — see [LICENSE](LICENSE)
