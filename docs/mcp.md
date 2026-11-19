# MCP reference

Start with `qd35_get_status` for hardware controls or `qd35_list_alarms` for scheduled wakeups.
MCP clients that support resources can read `qd35://guide` without scanning or connecting to Bluetooth.

For ordinary lighting, a single `qd35_set_light` call with
`{"effect":"static","rgb":"#FF6A20","brightness":20}` sets a warm static light at 20%.
Use the animation tools when you want to replace the installed Extended asset.
For sunrise, provide the target wake time (the end of the ramp), an IANA timezone, and the
days to run. See [alarm setup](alarm-clock.md) for the complete flow.

## Device and audio

| Tool | Purpose |
|---|---|
| `qd35_discover` | Bounded BLE scan, without pairing or connecting |
| `qd35_get_status` | Read all useful current state |
| `qd35_get_capabilities` | Show supported, excluded, and gated operations |
| `qd35_get_events` | Read retained notifications, including playback/title/artist events and raw unknown events |
| `qd35_set_volume` | Set and read back volume |
| `qd35_set_input` | Select `bluetooth`, `aux`, or `usb` |
| `qd35_set_eq_preset` | Select `music`, `game`, `movie`, or `diy` |
| `qd35_set_eq_band` | Set band 0–5 from -3 to +3 dB in 0.5 dB steps |
| `qd35_reset_eq` | Restore all six DIY bands to their firmware defaults |
| `qd35_media_control` | `play`, `pause`, `next`, or `previous` |
| `qd35_set_ldac` | Select `off`, `48khz`, or `96khz`; a change restarts Bluetooth |
| `qd35_rename_device` | Rename using the live D8-advertised UTF-8 limit (35 bytes on tested firmware) |

## Lighting and animations

| Tool | Purpose |
|---|---|
| `qd35_list_custom_lights` | List official downloads, creative presets, calibration patterns, and palettes |
| `qd35_apply_diagnostic_light` | Upload one low-brightness protocol-calibration animation |
| `qd35_apply_authored_light` | Compile and upload bounded local RGB frames; byte four is forced to zero |
| `qd35_apply_reactive_light` | Compile a sound-reactive asset from 10 bands × 10 intensity levels × 37 RGB colors |
| `qd35_apply_animation_preset` | Generate and upload `pac_man`, `comet`, `rainbow_wave`, `fireflies`, or `embers` |
| `qd35_set_light` | Change effect-specific RGB/color mode, global brightness, supported speed, or enabled state |
| `qd35_reset_light` | Reset one ordinary effect's parameters |
| `qd35_set_light_timer` | Set the lighting timer when the live D8 capability block advertises it |
| `qd35_apply_custom_light` | Download, verify, transfer, read back, and optionally activate an official Extended animation |

## Clock and alarms

| Tool | Purpose |
|---|---|
| `qd35_sync_clock` | Sync time and select `preserve`, `12h`, or `24h` |
| `qd35_set_timezone` | Persist an IANA timezone and optionally synchronize immediately |
| `qd35_list_alarms` | List persistent sunrise alarms and timer/runtime state |
| `qd35_create_sunrise_alarm` | Install optional bedtime preparation plus timezone-aware, reboot-persistent sunrise/audio timers |
| `qd35_run_alarm_now` | Start one installed alarm immediately |
| `qd35_stop_alarm` | Stop current light/audio playback without disconnecting Bluetooth |
| `qd35_snooze_alarm` | Stop audio, restore the fully dark night state, and restart with a short fade after 1–60 minutes |
| `qd35_delete_alarm` | Remove an alarm and its user timer/service |
| `qd35_set_alarm_audio_keepalive` | Enable or disable overnight Classic-audio reconnection |

## Patched display and firmware

| Tool | Purpose |
|---|---|
| `qd35_get_display_status` | Show locally retained patched-display state and readback limits |
| `qd35_get_display_firmware_info` | Show bundled patch/source hashes, gates, and fixed install/restore contract |
| `qd35_set_display` | Patched firmware: control display power, digits, `line`, status symbols, brightness, dial-follow, and raw mode. `logo` is a legacy alias for `line`. Display power controls the physical wordmark. |
| `qd35_write_display_grid_byte` | Patched firmware expert mode: write one of eight raw CMS1640 grid bytes |
| `qd35_reset_display` | Patched firmware: restore volatile stock-equivalent display defaults |
| `qd35_install_display_firmware` | Fetch the exact pinned Edifier source, apply the bundled patch, and flash the pinned QD35 |
| `qd35_restore_stock_firmware` | Fetch and flash the exact pinned official image, removing the display patch |

## Disruptive device operations

| Tool | Purpose |
|---|---|
| `qd35_disconnect_audio` | Drop the Classic Bluetooth audio link |
| `qd35_power_off` | Remote shutdown; remote power-on is not supported |
| `qd35_factory_reset` | Clear pairings and settings |

## Interpreting results

A result with `applied: true` passed the operation's stated verification.
Some operations can report an acknowledgement, a requested state, or an unknown outcome.
Clock sync has no independent readback, and an ordinary lighting response from the display
patch cannot verify a display change. Read the reported outcome before retrying. Transport
and validation errors also surface as MCP tool errors.

## Operation limits

Lighting controls are effect-specific: `ambient` and `magic_tides` accept `color_mode` values `light`/`deep`; `shooting_star`, `breathing`, `tempo_emo`, and `static` accept `rgb`; `extended` accepts `off`/`on`. Brightness is global on firmware 1.3.0. Speed is available only when status reports a numeric speed.

Custom-light downloads are restricted to fixed Edifier HTTPS URLs, capped in size, and checked against pinned SHA-256 hashes and the recovered 150-byte record format before Bluetooth transfer. Firmware, target identity, and one-write ATT capacity are verified before `79`; transfer limits come from the speaker, every chunk is acknowledged with bounded retries, failure triggers a best-effort abort, and completion is checked with `78` identity readback. Firmware does not return the installed binary, so the result states that verification limit explicitly. Diagnostic patterns are generated locally from fixed, validated geometry and activate at no more than 25% brightness. Authored animations accept only inline `#RRGGBB` frames: 1–102 frames, exactly 37 colors per frame, 5–60,000 ms per frame, and a ten-minute loop bound. They cannot select files or network locations, and byte four is always zero.

The diagnostic catalog includes the measured front-view emission centroid for every wire
address. These coordinates locate the visible light-guide output. See the
[address map](address-map.md) for the schematic, measurement method, and 3D limits.

LDAC changes can restart Bluetooth and invalidate the host's Classic bond. Reconnect or re-pair
Classic audio and read `qd35_get_status` to verify the requested mode.
See the [display patch guide](display-firmware.md) for fixed-image firmware operations.

## Configuration

| Variable | Default | Meaning |
|---|---:|---|
| `QD35_TRANSPORT` | `bleak` | `bleak` for hardware or `fake` for simulation |
| `QD35_DEVICE` | auto | BLE address allowlist/target |
| `QD35_ADAPTER` | system default | Optional BlueZ adapter such as `hci1` |
| `QD35_ENABLE_WRITES` | `0` | Enable ordinary state changes |
| `QD35_ENABLE_DANGEROUS` | `0` | Enable reset, power-off, and audio disconnect |
| `QD35_CUSTOM_DISPLAY_FIRMWARE` | `0` | Allow private display commands after installing the exact patch |
| `QD35_ENABLE_FIRMWARE_INSTALL` | `0` | Enable fixed patch-install and stock-restore MCP tools; both write gates also apply |
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
