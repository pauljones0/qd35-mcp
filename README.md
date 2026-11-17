<p align="center">
  <img src="assets/icon.svg" width="96" height="96" alt="QD35 MCP icon">
</p>

# QD35 MCP

A local [Model Context Protocol](https://modelcontextprotocol.io/) server that lets an agent control an **Edifier QD35** over Bluetooth Low Energy. It talks directly to the speaker; the discontinued Edifier Connect mobile app is not required.

The protocol is independently implemented from observed behavior and Edifier Connect command definitions. It has been exercised against a physical QD35 running firmware 1.3.0. This project is not affiliated with Edifier.

## What it controls

- Read complete status: identity, firmware, decoded capabilities, volume, input, EQ, lights, playback, audio-decoding status, custom-light status, and LDAC
- Set volume using the speaker-reported range (firmware 1.3.0 reports 0–16)
- Select Bluetooth, AUX, or USB input
- Select Music, Game, Movie, or DIY EQ
- Adjust or reset the six DIY bands: 62 Hz, 250 Hz, 1 kHz, 4 kHz, 8 kHz, and 16 kHz
- Select or reset lighting effects; use RGB on effects 3–6, named color modes on effects 1, 2, and 7, and change global brightness, supported speed, or on/off state
- Install all four official Extended animations (Gradient, Water-drop, Spiral, and Multicolored), with palette support where the app provides it
- Upload bounded address-mapping, RGB, timing, and fourth-component diagnostic animations for video-assisted reverse engineering
- Probe Spiral's firmware-generated type-3 motion with bounded color and timing diagnostics
- Read the calibrated front-view visible-emission position for all 37 wire addresses
- Compile and upload local RGB animations with 1–102 frames, exactly 37 colors per frame, bounded timing, and no file/URL input
- Retain and expose unsolicited playback/title/artist events instead of discarding them
- Play/pause and skip the active Bluetooth player
- Sync the clock and choose 12/24-hour display
- Select LDAC off, 48/44.1 kHz, or 96 kHz
- Rename, disconnect Bluetooth audio, power off, or factory-reset the speaker
- Install persistent sunrise alarms that ramp light and speaker volume, play local audio or public internet radio, and keep paired Classic audio connected overnight

The app's 32 built-in BGM tracks are audio files played by the phone, not stored speaker commands, so they are not redistributed. Firmware update is intentionally excluded from the agent interface.

## Requirements

- Linux with BlueZ and a working Bluetooth adapter
- Python 3.11+
- A powered-on QD35 nearby

The BLE control channel does not require pairing. Classic Bluetooth pairing is still needed for audio playback.

## Install and run

Using `uv`:

```bash
uv tool install .
QD35_ENABLE_WRITES=1 qd35-mcp
```

For development:

```bash
uv sync --extra dev
QD35_TRANSPORT=fake uv run qd35-mcp
```

The fake transport is deterministic and useful for checking an MCP host without a speaker. It proves the server contract, not Bluetooth compatibility.

## MCP host configuration

Use an absolute executable path when a desktop host has a restricted `PATH`:

```json
{
  "mcpServers": {
    "qd35": {
      "command": "/home/you/.local/bin/qd35-mcp",
      "env": {
        "QD35_ENABLE_WRITES": "1"
      }
    }
  }
}
```

To enable reset, shutdown, and audio disconnect, also set `QD35_ENABLE_DANGEROUS=1`. MCP host approval dialogs are still useful; the environment gate is defense in depth.

If exactly one QD35 is visible, it is selected automatically. Otherwise set `QD35_DEVICE` to its BLE address:

```json
"QD35_DEVICE": "AA:BB:CC:DD:EE:FF"
```

Do not copy that example address unless it is your speaker.

## Tools

| Tool | Purpose |
|---|---|
| `qd35_discover` | Bounded BLE scan, without pairing or connecting |
| `qd35_get_status` | Read all useful current state |
| `qd35_get_capabilities` | Show supported, excluded, and gated operations |
| `qd35_get_events` | Read retained notifications, including playback/title/artist events and raw unknown events |
| `qd35_list_custom_lights` | List official QD35 Extended animations and palettes |
| `qd35_apply_diagnostic_light` | Upload one low-brightness protocol-calibration animation |
| `qd35_apply_authored_light` | Compile and upload bounded local RGB frames; byte four is forced to zero |
| `qd35_set_volume` | Set and read back volume |
| `qd35_set_input` | Select `bluetooth`, `aux`, or `usb` |
| `qd35_set_eq_preset` | Select `music`, `game`, `movie`, or `diy` |
| `qd35_set_eq_band` | Set band 0–5 from -3 to +3 dB in 0.5 dB steps |
| `qd35_reset_eq` | Restore all six DIY bands to their firmware defaults |
| `qd35_set_light` | Change effect-specific RGB/color mode, global brightness, supported speed, or enabled state |
| `qd35_reset_light` | Reset one ordinary effect's parameters |
| `qd35_set_light_timer` | Set the lighting timer when the live D8 capability block advertises it |
| `qd35_apply_custom_light` | Download, verify, transfer, read back, and optionally activate an official Extended animation |
| `qd35_media_control` | `play`, `pause`, `next`, or `previous` |
| `qd35_sync_clock` | Sync time and select `preserve`, `12h`, or `24h` |
| `qd35_set_timezone` | Persist an IANA timezone and optionally synchronize immediately |
| `qd35_list_alarms` | List persistent sunrise alarms and timer/runtime state |
| `qd35_create_sunrise_alarm` | Install optional bedtime preparation plus timezone-aware, reboot-persistent sunrise/audio timers |
| `qd35_run_alarm_now` | Start one installed alarm immediately |
| `qd35_stop_alarm` | Stop current light/audio playback without disconnecting Bluetooth |
| `qd35_delete_alarm` | Remove an alarm and its user timer/service |
| `qd35_set_alarm_audio_keepalive` | Enable or disable overnight Classic-audio reconnection |
| `qd35_set_ldac` | Select `off`, `48khz`, or `96khz`; a change restarts Bluetooth |
| `qd35_rename_device` | Rename using the live D8-advertised UTF-8 limit (35 bytes on tested firmware) |
| `qd35_disconnect_audio` | Drop the Classic Bluetooth audio link |
| `qd35_power_off` | Remote shutdown; remote power-on is not supported |
| `qd35_factory_reset` | Clear pairings and settings |

Lighting controls are effect-specific: `ambient` and `magic_tides` accept `color_mode` values `light`/`deep`; `shooting_star`, `breathing`, `tempo_emo`, and `static` accept `rgb`; `extended` accepts `off`/`on`. Brightness is global on firmware 1.3.0. Speed is available only when status reports a numeric speed.

Custom-light downloads are restricted to fixed Edifier HTTPS URLs, capped in size, and checked against pinned SHA-256 hashes and the recovered 150-byte record format before Bluetooth transfer. Firmware, target identity, and one-write ATT capacity are verified before `79`; transfer limits come from the speaker, every chunk is acknowledged with bounded retries, failure triggers a best-effort abort, and completion is checked with `78` identity readback. Firmware does not return the installed binary, so the result states that verification limit explicitly. Diagnostic patterns are generated locally from fixed, validated geometry and activate at no more than 25% brightness. Authored animations accept only inline `#RRGGBB` frames: 1–102 frames, exactly 37 colors per frame, 5–60,000 ms per frame, and a ten-minute loop bound. They cannot select files or network locations, and byte four is always zero.

The diagnostic catalog includes the measured front-view emission centroid for every wire address. These coordinates locate the visible light-guide output, not an unseen LED package. See [docs/address-map.md](docs/address-map.md) for the schematic, measurement method, and 3D limits.

Setting tools read state back when the protocol allows it. Media `C2` is one-way, but the tool also queries `C3` playing state; that can verify play/pause, not track changes. Clock sync reports a device success acknowledgement but `applied` remains unknown because the protocol has no clock readback. A timeout, malformed confirmation, or connection loss after any write is reported as an uncertain outcome. Firmware 1.3.0 restarts Bluetooth after an LDAC change and may invalidate the tested host bond, so re-pair Classic audio and call `qd35_get_status` to verify the requested mode. The tool avoids the restart entirely when the requested mode is already active.

## Configuration

| Variable | Default | Meaning |
|---|---:|---|
| `QD35_TRANSPORT` | `bleak` | `bleak` for hardware or `fake` for simulation |
| `QD35_DEVICE` | auto | BLE address allowlist/target |
| `QD35_ADAPTER` | system default | Optional BlueZ adapter such as `hci1` |
| `QD35_ENABLE_WRITES` | `0` | Enable ordinary state changes |
| `QD35_ENABLE_DANGEROUS` | `0` | Enable reset, power-off, and audio disconnect |
| `QD35_SCAN_TIMEOUT` | `8` | Discovery time in seconds |
| `QD35_REQUEST_TIMEOUT` | `4` | Per-command response timeout |
| `QD35_TIMEZONE` | persisted/system | Override automatic IANA timezone resolution |
| `QD35_TIMEZONE_STATE_PATH` | XDG state dir | Override the mode-0600 persistent timezone file |
| `QD35_ALARM_ALLOW_REMOTE_AUDIO` | `0` | Permit public HTTP(S) alarm streams after public-IP validation |
| `QD35_ALARM_ALLOWED_HOSTS` | unset | Comma-separated exact host allowlist for remote alarm streams |
| `QD35_ALARM_MEDIA_DIR` | unset | Directory containing allowed local alarm audio files |
| `QD35_ALARM_STATE_PATH` | XDG state dir | Override persistent alarm JSON location |
| `QD35_ALARM_UNIT_DIR` | XDG user units | Override systemd user-unit directory, primarily for tests |

The first timezone is resolved from `QD35_TIMEZONE`, then the persisted MCP state, then the operating system. `qd35_set_timezone` updates that persistent state so container or service-manager timezone differences cannot silently shift future speaker clock syncs. Alarm creation also persists its named timezone. Logs go to stderr; stdout remains reserved for MCP messages.

## Troubleshooting

- **No device found:** keep the speaker powered on and close Edifier Connect. The BLE advertisement may have no local name, so name-only scanners miss it. If it remains absent after a Bluetooth restart, reconnect the separate Classic audio device (`EDIFIER QD35`); firmware 1.3.0 resumed its BLE control advertisement after the Classic link returned.
- **Permission error:** ensure the user session can access BlueZ over D-Bus. Test with `bluetoothctl scan le`.
- **BlueZ 5.87 crashes during discovery:** update to BlueZ 5.88 or newer. BlueZ 5.87 has a known UUID-filter crash; this server avoids it by scanning broadly and filtering `a400` in-process.
- **Device busy:** only one controller may hold the BLE connection. Close the phone app and retry.
- **LDAC disconnect:** expected. On tested firmware, an actual LDAC change invalidated the old Classic bond. Remove that stale host pairing, pair again, then call `qd35_get_status`. The speaker itself recovered without a physical power-cycle.
- **Audio is not connected:** BLE control and Classic Bluetooth audio are separate links.

See [docs/alarm-clock.md](docs/alarm-clock.md) for pairing, sunrise behavior, stream security, persistence, and operational limits.

See [docs/app-features.md](docs/app-features.md), [docs/protocol.md](docs/protocol.md), and [docs/hardware-validation.md](docs/hardware-validation.md) for the evidence and current limits.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
uv build
```

Physical checks are opt-in and never run in CI:

```bash
QD35_HARDWARE=1 QD35_DEVICE=<ble-address> uv run pytest tests/hardware
QD35_HARDWARE=1 QD35_HARDWARE_WRITES=1 QD35_ENABLE_WRITES=1 \
  QD35_DEVICE=<ble-address> uv run pytest tests/hardware
```

Custom-light hardware validation is separately gated because it replaces the installed Extended asset (the visible ordinary effect is restored afterward):

```bash
QD35_HARDWARE=1 QD35_HARDWARE_WRITES=1 QD35_HARDWARE_CUSTOM_LIGHTS=1 \
  QD35_ENABLE_WRITES=1 QD35_DEVICE=<ble-address> \
  uv run pytest tests/hardware -k custom_light
```

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) and do not submit proprietary app assets, personal Bluetooth identifiers, or unverified command mappings as facts.

## License

MIT. See [LICENSE](LICENSE).
