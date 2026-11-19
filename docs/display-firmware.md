# Optional front-display firmware

QD35 firmware 1.3.0 hard-codes the clock/status/logo CMS1640 display to maximum
brightness and exposes no independent Bluetooth control. This project includes a
small, original Cortex-M3 patch that adds those controls.

This is an advanced, potentially device-bricking operation. Keep power connected,
pin `QD35_DEVICE` to the intended BLE address, and retain the exact official image
for rollback. The MCP cannot accept a path, URL, or arbitrary image from an agent.
It exposes only fixed-purpose install/restore tools when the operator explicitly
enables all three firmware gates.

## What the patch changes

The patch hooks model- and firmware-specific sites:

- unused lighting effect zero becomes a private command namespace protected by a
  `0x35` magic byte;
- both eight-byte CMS1640 frame paths apply the volatile masks and retain the stock
  display-write guards;
- frame writes and the stock wake path use the selected display power and brightness
  without briefly issuing a maximum-brightness command;
- reset reserves the top 24 SRAM bytes above the shortened stack and clears the
  private state marker.

The 452-byte code occupies `0x0800BE20..0x0800BFE3`. Animation storage starts at
`0x0800C000`, 28 bytes after the end of the patch. The reserved SRAM starts at `0x20002718`. Each MCU
reset restores display, clock, logo, and symbols on at brightness 7, with
dial-follow and raw mode off.

The implementation exposes:

- whole display power without changing Bluetooth or audio state;
- clock-digit visibility;
- a mask for the stock horizontal-line group, exposed as `logo` in the API;
- remaining status-symbol visibility;
- manual CMS1640 brightness 0–7;
- brightness following the ordinary 0–100 lighting value changed by the right-side
  lighting control or `qd35_set_light`;
- expert raw access to each of the eight CMS1640 grid bytes.

The stock BLE protocol cannot query this private SRAM state. MCP results report
the command acknowledgement and the requested values. Optical brightness and
visible segments require visual confirmation. In the live isolation check, the
wordmark stayed lit with all eight exposed segment bytes zeroed. It went dark
with the display driver off. The `logo` setting masks the horizontal-line bits
in the clock area. Independent control of the physical wordmark remains open.

## Reproducible build

Obtain the official image from Edifier's public firmware URL recorded in
`firmware-analysis.md`. The builder refuses anything except:

```text
size:    40471 bytes
SHA-256: 91640d9839e7e0542c5618a0781bc8173915b81a8f2a69173103618993c849b9
```

Build without redistributing the source image:

```bash
PYTHONPATH=src python tools/build_display_firmware.py \
  qd35-1.3.0.bin qd35-1.3.0-display-control.bin
```

The deterministic result is 40,932 bytes with SHA-256:

```text
b2354c561dd02e73e8d8f25599b956d69e58742421f8edc13101a3f7f0786c8a
```

`firmware/qd35_display_patch.S` is the readable source. The patcher validates the
stock hash, original hook bytes, code bounds, and final hash before writing output.
The stock path and output path must identify different files. The builder writes
the complete patch to a temporary file before replacing the destination.
Run the instruction-level checks against the generated image with
`uv run --with unicorn python tools/check_display_firmware.py qd35-1.3.0.bin`.

## Install and rollback

The local CLI is useful for an already-retained official image and requires all
safety gates plus an explicit address:

```bash
QD35_DEVICE=<speaker-ble-address> \
QD35_ENABLE_WRITES=1 \
QD35_ENABLE_DANGEROUS=1 \
QD35_ENABLE_FIRMWARE_INSTALL=1 \
QD35_CUSTOM_DISPLAY_FIRMWARE=1 \
PYTHONPATH=src python tools/install_display_firmware.py qd35-1.3.0.bin
```

It fingerprints the live QD35, validates installed version 1.3.0, patches the
user-supplied image in memory, and reproduces Edifier Connect 8.5.5's OTA v2 flow.
The device chooses its transfer size; the tested speaker requested 128-byte chunks.
Frames larger than the 500-byte BLE transport size are split exactly like the app,
with a 300 ms gap. The installer checks ATT write capacity before entering OTA
mode and allows at most five repeated requests for the same block.
It also listens for retries or errors for three seconds after sending the final
block. Silence or a disconnect during that window leaves completion unverified.
The result reports the hash of the transmitted image.

To restore the exact stock image:

```bash
QD35_DEVICE=<speaker-ble-address> \
QD35_ENABLE_WRITES=1 \
QD35_ENABLE_DANGEROUS=1 \
QD35_ENABLE_FIRMWARE_INSTALL=1 \
PYTHONPATH=src python tools/install_display_firmware.py \
  --restore-stock qd35-1.3.0.bin
```

After installation, wait for the control advertisement, enable
`QD35_CUSTOM_DISPLAY_FIRMWARE=1` in the MCP host, and exercise a brightness-only
`qd35_set_display` call to check the new masked command. The OTA flow does not
provide an installed image hash. Record the command response and check the display
visually to verify its behavior.

After rollback, verify the version and stock behavior. Both images can return an
ordinary `6B` reply to the `D4` probe. The stock parser changes RGB brightness
to the payload's level and the patch leaves brightness unchanged. See the
[live rollback test](hardware-validation.md#stock-rollback-2026-11-19-utc).

For other users, the installed MCP package is self-contained for the original patch
code. The 452-byte patch is embedded in `qd35_mcp.firmware_patch`. The fixed
MCP tools need no file argument:

```text
qd35_install_display_firmware(confirmation="INSTALL DISPLAY PATCH")
qd35_restore_stock_firmware(confirmation="RESTORE STOCK FIRMWARE")
```

They require `QD35_DEVICE`, `QD35_ENABLE_WRITES=1`,
`QD35_ENABLE_DANGEROUS=1`, and `QD35_ENABLE_FIRMWARE_INSTALL=1`; installation also
requires `QD35_CUSTOM_DISPLAY_FIRMWARE=1`. They fetch only the hard-coded official
Edifier HTTPS URL, reject redirects and wrong sizes/hashes, and never accept an
agent-selected source. If Edifier's host is temporarily unavailable, the bounded
MCP download fails without starting OTA; the local installer can still use a
previously retained exact official image.

## MCP behavior

`qd35_set_display` applies only supplied fields. A complete request still uses
the original full-state command. Partial updates need the current patch, which
merges changes atomically in firmware across separate MCP clients. `brightness`
is 0–7. When `follow_lighting_dial=true`, ordinary lighting brightness maps to
those eight hardware levels and zero turns the display off. Bluetooth remains
active.

Raw mode is expert-only and separately requires `QD35_ENABLE_DANGEROUS=1`:

1. write desired bytes with `qd35_write_display_grid_byte(index, value)`;
2. call `qd35_set_display(raw_mode=true)`;
3. use `qd35_reset_display` to restore stock-equivalent behavior.

Semantic clock/logo/symbol masks still apply on top of raw data. The raw interface
exposes the eight working display bytes. The chip has 16 bytes of display RAM,
and stock initialization clears all 16. Clock separators remain visible when
the clock is enabled and both the logo and status symbols are disabled.

## Visual isolation check

For a comparison on the current patch, turn off the RGB lighting and use fixed
camera exposure in a dark room. Compare these states at one fixed display
brightness and record the wordmark and clock together.

The local runner `tools/check_display_hardware.py` shows the normal clock as a
start marker, then cycles the states below with elapsed timestamps. For a
40-second clip, use `--repeat 2 --hold-seconds 1.5` for two passes with 19.5 seconds
of display holds plus Bluetooth command time. Use `--step` with an observer and
press Enter between phases. After three minutes without input, the runner restores
the settings and exits.
Use the explicit speaker address and enable ordinary writes, patched display
control, and dangerous writes as for other raw display operations. It restores
the original RGB brightness and effect and returns the
front display to its firmware defaults after the comparison.

| State | What it isolates |
|---|---|
| Driver off | Ambient reflection baseline |
| Driver on with eight zero bytes | Illumination with all eight exposed segment bytes zeroed |
| Digits-only raw frame `00 1E 19 03 1F 3D 27 1F` | Wordmark appearance when only digits are commanded |
| Horizontal-line frame `60 40 40 40 40 40 40 40` | The line in the clock area controlled by the field named `logo` |
| Second startup group `00 00 00 00 00 00 80 80` | The small striped indicator in the clock area |

The wordmark was lit in every driver-on phase, including the eight-zero-byte
frame. The raw test writes `C0` through `C7`. In emulation, the existing wake
routine at `0x08006D50` cleared all sixteen bytes before enabling the display,
and its subsequent redraws wrote only the first eight. The wordmark remained
visible in the eight-zero-byte frame after the owner power-cycled the speaker.
`--step --logo-pwm` holds the same zero frame at brightness 0 and then 7. The
wordmark followed those brightness changes with the clock blank. A pass on the
physical board would trace the wordmark pins to the driver, supply, or other
control circuitry and capture the display bus during a full clear.

Edifier's [FCC internal photos for Z9G-EDF198](https://fccid.io/Z9G-EDF198),
exhibit 6311856 page 7, show both faces of the international display board.
The clock and EDIFIER wordmark occupy separate soldered modules on the board.
The photos show the pin rows, but the traces under the modules cannot be
followed reliably. The photos do not establish which driver or supply pins
power the wordmark.

## Earlier live validation

On 2026-11-17, the previous 40,880-byte revision transferred in 320 chunks of up
to 128 bytes. After the lighting MCU restarted, the following returned the stock
`SET_LIGHT` response payload `01 07 01 00 00 14 FF`:

- clock off with logo/symbols on;
- logo-grid off with clock/symbols on (the command was acknowledged and the
  physical wordmark remained visible);
- symbols off with clock/logo on;
- whole display off;
- manual brightness 0, 3, and 7;
- raw writes to indexes 0 through 7 and raw all-segments display;
- safe reset;
- right-side-lighting-control follow mode.

Ordinary lighting brightness was then changed to 0, 100, and restored to 20 with
live `6A` readback. Firmware query, volume, Bluetooth input, normal Extended lighting,
and BLE control remained healthy. No audio-disconnect command was issued.
