# Optional front-display firmware

QD35 firmware 1.3.0 hard-codes the clock/status/logo CMS1640 display to maximum
brightness and exposes no independent Bluetooth control. This project includes a
small, original Cortex-M3 patch that adds those controls. It was installed and
validated on the owner's international QD35 on 2026-11-17.

This is an advanced, potentially device-bricking operation. Keep power connected,
pin `QD35_DEVICE` to the intended BLE address, and retain the exact official image
for rollback. The MCP cannot accept a path, URL, or arbitrary image from an agent.
It exposes only fixed-purpose install/restore tools when the operator explicitly
enables all three firmware gates.

## What the patch changes

The patch hooks two model/firmware-specific sites:

- unused lighting effect zero becomes a private command namespace protected by a
  `0x35` magic byte;
- the common eight-byte CMS1640 writer applies volatile masks and brightness before
  writing the display.

The 400-byte code occupies `0x0800BE20..0x0800BFAF`. Animation storage begins at
`0x0800C000`, leaving 80 bytes of separation. State uses otherwise-unreferenced SRAM
at `0x20001730`; it is not written to flash. Every speaker power cycle therefore
returns to the safe stock-equivalent default: display, clock, logo, and symbols on,
brightness 7, dial-follow off, raw mode off.

The implementation exposes:

- whole display power without changing Bluetooth or audio state;
- clock-digit visibility;
- EDIFIER-logo visibility;
- remaining status-symbol visibility;
- manual CMS1640 brightness 0–7;
- brightness following the ordinary 0–100 lighting value changed by the right-side
  lighting control or `qd35_set_light`;
- expert raw access to each of the eight CMS1640 grid bytes.

The stock BLE protocol cannot query this private SRAM state. MCP results therefore
report a firmware acknowledgement and locally retained desired state, not an
independent physical readback. Visual confirmation remains authoritative.

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

The deterministic result is 40,880 bytes with SHA-256:

```text
f58b6cd010f7a3073d8f2f0e50685eb94599f6c57b034e44977984f5f24c6638
```

`firmware/qd35_display_patch.S` is the readable source. The patcher validates the
stock hash, original hook bytes, code bounds, and final hash before writing output.

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
with a 300 ms gap.

To restore the exact stock image:

```bash
QD35_DEVICE=<speaker-ble-address> \
QD35_ENABLE_WRITES=1 \
QD35_ENABLE_DANGEROUS=1 \
QD35_ENABLE_FIRMWARE_INSTALL=1 \
PYTHONPATH=src python tools/install_display_firmware.py \
  --restore-stock qd35-1.3.0.bin
```

Neither OTA mode can read back an installed image hash. After installation, wait for
the control advertisement, enable `QD35_CUSTOM_DISPLAY_FIRMWARE=1` in the MCP host,
and exercise `qd35_set_display`. A valid private-command acknowledgement is the
protocol-level proof that the patch is executing.

For other users, the installed MCP package is self-contained for the original patch
code: the exact 400-byte payload is embedded in `qd35_mcp.firmware_patch`. The fixed
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

`qd35_set_display` always sends the complete semantic state. `brightness` is 0–7;
when `follow_lighting_dial=true`, the current ordinary lighting brightness maps to
those eight hardware levels and a lighting value of zero turns the display off.
Bluetooth remains active.

Raw mode is expert-only and separately requires `QD35_ENABLE_DANGEROUS=1`:

1. write desired bytes with `qd35_write_display_grid_byte(index, value)`;
2. call `qd35_set_display(raw_mode=true)`;
3. use `qd35_reset_display` to restore stock-equivalent behavior.

Semantic clock/logo/symbol masks still apply on top of raw data. The raw interface
exposes every driver bit without pretending that every status-icon bit has already
been physically named.

## Live validation

On the tested QD35, the patched image transferred as 320 acknowledged 128-byte
chunks. After the lighting MCU restarted, the following all returned the stock
`SET_LIGHT` response payload `01 07 01 00 00 14 FF`:

- clock off with logo/symbols on;
- logo off with clock/symbols on;
- symbols off with clock/logo on;
- whole display off;
- manual brightness 0, 3, and 7;
- raw writes to indexes 0 through 7 and raw all-segments display;
- safe reset;
- right-side-lighting-control follow mode.

Ordinary lighting brightness was then changed to 0, 100, and restored to 20 with
live `6A` readback. Firmware query, volume, Bluetooth input, normal Extended lighting,
and BLE control remained healthy. No audio-disconnect command was issued.
