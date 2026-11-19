# QD35 firmware analysis

This document records independently derived facts from Edifier's official QD35
firmware and the Edifier Connect application. Addresses below use image base
`0x08002000`. They describe QD35 firmware 1.3.0 only unless stated otherwise.
The [firmware postmortem](firmware-postmortem.md) covers the patch history,
board capability boundaries, and candidates for future firmware work.

## Provenance and update status

Edifier Connect queries `GET /home/api/v1/ota-upgrade` with `timestamp`, `os`,
`product_id`, `mac`, `current_version`, and `app_version`. It computes `sign` by
sorting the unsigned parameters by name, concatenating `name=value`, appending the
static application key, and taking uppercase MD5. The relevant application code is
`OTAHelper.getOTAUpdateData`, `SignTool.sign`, and `Api.getOTAUpdateData` in Edifier
Connect 8.3.43. Product 223 is QD35.

Replaying that application flow recovered Edifier's QD35 1.3.0 image from:

```text
https://edifier-provider-oss.edifier.com/product_firmware/20230221/2855917202cfc6cbefd62320920c3015.bin
```

| Property | Value |
|---|---|
| Size | 40,471 bytes |
| SHA-256 | `91640d9839e7e0542c5618a0781bc8173915b81a8f2a69173103618993c849b9` |
| Initial stack pointer | `0x20002730` |
| Reset vector | `0x0800B2F5` (Thumb; function address `0x0800B2F4`) |
| Image base | `0x08002000` |

The vector table and code identify this as the lighting/control Cortex-M image, not
the BES Bluetooth SoC firmware. A live OTA query on 2026-11-17 for product 223 at
installed version 1.3.0 returned HTTP success with an empty update object. The
speaker therefore has the newest version currently offered by that API. A historical
control query also returned empty during the final check even though the 1.3.0 image
URL remains downloadable, so the API must not be treated as a permanent archive.

Edifier Connect's QD35 path uses v2 opcode `10`. For the single-image OTA type, its
start payload is the image size as a big-endian 32-bit integer, the three target
version bytes, and a big-endian block size of 512. Other products can use a 13-byte
container header with separate MCU and Bluetooth image lengths; the recovered QD35
file is a raw single Cortex-M image and does not have that header. The application
also changes the NEW-X/QD35 lighting state before starting the transfer.

This is sufficient to retrieve, inspect, exact-hash validate, and transfer the
correct image. The optional display patch now uses that recovered single-image OTA
flow and includes an exact-stock restore mode. Power-loss recovery is still not
established, so installation remains separately and explicitly gated.

## Custom-animation storage path

The Bluetooth SoC sends the lighting MCU an ASCII-prefixed `+DIY` protocol. The
firmware recognizes `+DIY+GET`, `+DIY+RDY`, `+DIY+RUN`, and `+DIY+END` at
`0x080037F0` through `0x0800399E`.

For `+DIY+RUN`, the receive loop at `0x080038C6` consumes 75 incoming byte pairs.
Each pair is combined as a big-endian 16-bit value and written as a native
little-endian halfword to external animation storage beginning at `0x0800C000`.
This proves that the downloadable file format is big-endian where it has 16-bit
fields; the apparently reversed halfword loads in the renderer are not a channel
swap.

One record is exactly 150 bytes:

```text
duration_ms_be16
37 x [R, G, B, reserved]
```

The live receiver reports a 15,360-byte store, so at most 102 complete records fit.
All known Edifier assets keep `reserved` at zero. Locally authored assets do the
same.

## Renderer and type dispatch

The dispatch at `0x08008494` reads the uploaded type and has exactly three branches:

| Type | Function | Firmware behavior |
|---:|---:|---|
| 1 | `0x08005F34` | Literal frame sequence |
| 2 | `0x08006074` | Audio-reactive frame selection/merge |
| 3 | `0x080059A4` | Firmware-generated Spiral motion |

There is no type-4 branch. Unknown values reach none of these renderers.

The common pixel writer at `0x08004E04` accepts R, G, B, logical index, and
brightness. It bounds the index to `< 37`, scales each channel by brightness/100,
and writes exactly three bytes to the output buffer at `0x2000162C`. The clear and
fill routines at `0x08004FE4`, `0x08004E64`, and `0x08004E92` also iterate exactly
37 logical slots.

The next stage removes an important ambiguity. `0x08004CA4` converts all 37 RGB
triples into 37 consecutive 24-bit one-wire PWM words. A zero bit is encoded as
`0x0E` and a one bit as `0x1C`. The three source bytes occupy successive eight-bit
windows in each 24-bit word. The serialized buffer has 87 leading reset slots,
888 data slots (`37 * 24`), and a final zero; `0x08008DF2` starts a DMA transfer of
exactly `0x3D0` (976) slots. This is the waveform generator for a WS2812-style
addressable RGB chain, not a software-only array.

The owner's synchronized type-1 recording produced a distinct controlled emission
for every address 0 through 36. On the tested international QD35, all 37 transmitted
nodes therefore have a visible downstream effect. Addresses 0 through 19 traverse
the lower light guide from left to right. Addresses 20 through 36 traverse the ring
around the mid-bass driver. The published coordinates are visible light-guide
centroids, not hidden package coordinates.

The [NEW-X teardown](https://www.52audio.com/archives/167663.html) counts 20 RGB
packages on the lower display PCB. That count covers one board. The international
QD35 sends 37 RGB words, and 37 corresponding positions lit in the synchronized
recording, including 17 around the mid-bass driver. The recording establishes
visible positions, while exact package locations require panel-level evidence.

### Type 1

`0x08005F34` reads one 150-byte record at `base + frame_index * 150`. It loads the
first halfword as the countdown and loops over 37 entries, passing only R, G, and B
to the common pixel writer. The reserved byte is never loaded. A nonzero countdown
is decremented by the periodic scheduler. The video calibration and this countdown
agree that the field is milliseconds, with a few milliseconds of scheduler/render
overhead per frame on the tested unit.

A zero duration is special: firmware replaces it with `0xFFFF` and then treats that
value as a non-decrementing sentinel. It holds the selected frame indefinitely rather
than producing a zero-length frame. The public compiler deliberately requires a
positive duration and does not expose this sentinel.

### Type 2

`0x08006074` uses 100 records in band-major order. It reads one sample for each
of ten audio-analysis channels and selects one of that band's ten frames.
The frame index is `10 * band + level`. The nine boundaries before the highest
level are 20, 55, 145, 320, 500, 800, 1227, 1699, and 2737 in the firmware's
internal units. Each new analysis sample moves the current band level at most
one step toward the target. The relationship between channels and frequencies
depends on the audio processor and has not been measured on the physical unit.

The renderer merges the ten selected frames and writes 37 RGB triples. Earlier
bands win when both frames have a nonzero value in the same 16-bit word. R/G
and B/reserved are merged separately. Use disjoint LEDs for predictable band
colors. The two-byte header does not set the frame duration. Authored assets
use the official header value 100 and zero all reserved bytes.

The new Spectrum Meter passed all 100 band and level cases in the original
firmware renderer under CPU emulation, with the full 37-pixel output checked.

### Type 3

`0x080059A4` reads the first 150-byte record once, extracts only record 0's R, G, and
B, and feeds that color into firmware-generated ring and lower-strip motion. It does
not use the two-byte record header, the reserved byte, or RGB records 1 through 36.
The official Spiral file repeats the same `FF FF FF 00` record 37 times, but 36 of
those repetitions are redundant on QD35 1.3.0.

This explains why the visual path could not be recovered by mapping the 37 payload
entries: the path is compiled into firmware, not encoded in the Spiral asset.

## Lighting and display hardware

The RGB chain and front displays are independent outputs.

### Addressable RGB lighting

The RGB chain is the only lighting surface reached by `+LED+GET`, `+LED+RST`, and
`+LED+SET`. The `+LED+SET` parser begins at `0x080032D0`. It accepts effect IDs 1
through 7, stores per-effect RGB or mode fields, clamps global brightness to 100,
and requests a lighting-state response. Effect 7 is Extended/custom lighting; the
downloaded types described above render through the same 37-node output chain.
Brightness zero or an effect's off selector can blank this chain without powering
down the speaker or its Bluetooth connection.

### Clock, status icons, and logo

The clock/status glass and the EDIFIER wordmark use a separate CMS1640-compatible
driver, with an eight-byte working buffer at `0x200016B8`. The
[CMS1640 datasheet](https://www.mcu.com.cn/en/support-download/171.html) maps each
RAM address to a GRID pin and each bit to a SEG pin. The firmware clears the entire
16-byte driver RAM (`C0`–`CF`) at display initialization, and the two ordinary
frame paths update `C0`–`C7`. The bit-banged driver is at
`0x08008E76` through `0x0800900C`:

| Function | Address | Behavior |
|---|---:|---|
| write display RAM | `0x08008F28` | sends data command `0x40`, address/data, then display-on `0x8F` |
| clear all RAM | `0x08008F94` | writes 16 zeros starting at `0xC0` |
| display on | `0x08008FCA` | sends the supplied display-control byte; callers use `0x8F` |
| display off | `0x08008FEE` | sends `0x80` |

The firmware always enables this driver at maximum CMS1640 brightness (`0x8F`). It
never uses the intermediate brightness commands `0x88` through `0x8E`. The
datasheet defines a single, nonlinear PWM setting for the whole display. Its
lowest enabled setting is 1/16 of a scan pulse and its highest is 14/16.

The six numeric digit planes are bits 0–5 of bytes 1–7. The live time renderer at
`0x08006C1E` also sets byte 0 bits 0–3 for the clock separators. `0x0800689A`
writes one digit. The `+TIM+SET` handler at
`0x08003258` only changes hour, minute, second, and 12/24-hour state, commits those
values to the RTC, and redraws the display. There is no clock-enable, clock-
brightness, or clock-off field in that command.

The stock horizontal-line initialization routine at `0x08006A2E(1)` sets byte 0 to
`60` and bytes 1–7 to `40` when starting from zero. Its zero branch redraws
the buffer unchanged. A separate startup routine at `0x08006A80(1)` sets bit 7
in bytes 6 and 7. The live test identifies that group as the small striped
indicator. The FCC photos show the clock and wordmark in separate packages on a
common PCB. The connection between the wordmark and driver is still unknown.

The current patch masks the horizontal-line bits and the status plane separately.
The clock mask also clears its separator bits when digits are off. The previous
revision zeroed all of byte 0 when both logo and symbols were off. The current
revision preserves the separator bits whenever the clock is enabled.

The only production path that blanks both the clock and wordmark is speaker standby:
`0x08008382` clears the RGB output, serializes the black frame, and sends CMS1640
display-off `0x80`. Waking the speaker reinitializes the display and turns the
wordmark back on. Standby also takes the speaker out of its normal active Bluetooth
audio state, so it does not satisfy “clock off while Bluetooth stays connected.”

Consequently, unmodified stock firmware 1.3.0 supports these combinations:

| Requested state | Stock result |
|---|---|
| RGB off, Bluetooth active | supported |
| Clock synchronized or 12/24-hour format changed | supported |
| Clock off, Bluetooth active | not exposed |
| Logo off, clock on | not exposed |
| Clock/logo brightness adjustment | not exposed; hard-coded maximum |
| All lights and displays off | supported only through speaker standby |

The project's optional patch implements those formerly hypothetical driver controls. It
reuses otherwise-invalid lighting effect zero as a private, magic-guarded command
path, hooks the common display writer, and keeps its state in volatile SRAM. It can
mask digit, horizontal-line, and other symbol bits independently; issue display off; select
brightness commands `0x88..0x8F`; follow the ordinary 0–100 lighting value; and use
all eight working display bytes. Replaying the earlier bedside raw frame through
the exact previous and current images produced the same digits-only RAM write.
The earlier image then sent a full-brightness command before applying the
requested minimum. The recorded residual has several possible sources, including
light leaking from another part of the panel, electrical ghosting, or ambient
light reflecting from the printed wordmark. Its cause and the ability to make
the wordmark visually dark with the current patch remain open. Whole-display off
was confirmed in the earlier bedside test. See [display firmware](display-firmware.md)
for a controlled comparison on the speaker.

## Lighting-MCU command inventory

All printable command tokens in the image and every branch of the central parser
were inventoried. The lighting/control MCU accepts the following families from the
Bluetooth/audio controller:

```text
+COM+RDY  +POW+OFF  +RST+SET
+PMD      +MOD      +EFF      +VOL
+ADP      +TON      +SMP      +AUD      +DET      +AUX
+LED+GET  +LED+RST  +LED+SET
+EQL+GET  +EQL+RST  +EQL+SET  +EQO+SET
+TIM+SET  +BUG+ESD  +FFT+SET
+DIY+GET  +DIY+RDY  +DIY+RUN  +DIY+END
+OTA+RDY
```

There is no stock `DISPLAY`, `CLOCK OFF`, `LOGO`, segment-RAM, or display-brightness
command token or parser branch. Searches of Edifier Connect 8.4.33 and 8.5.5 also
found no corresponding QD35 command builder. This rules out a merely hidden app UI
control for the stock protocol. The optional open patch supplies the missing custom
path without claiming that it was present in Edifier's protocol.

### Internal functions with further room to explore

| Function | Evidence in QD35 1.3.0 | Coverage |
|---|---|---|
| Audio-reactive custom patterns | `+FFT+SET` at `0x08003568` consumes ten two-byte audio values, and the type-2 renderer selects and merges frames. | The official Multicolored asset, Spectrum Meter preset, and reactive authoring tool use type 2. The authored renderer output was checked in emulation. |
| Prompt-tone coordination | `+TON+STA` at `0x08002E98` forces amplifier register 3 to `03` in Bluetooth mode and selects a fixed prompt gain table. Emulation produced the same amplifier writes at media volumes 0, 10, and 16. `+TON+END` clears the prompt state after its gain call. | Setting media volume to zero is insufficient to guarantee a silent prompt. A prompt-specific control on this QD35's Bluetooth/audio controller remains unverified. |
| USB/audio and AUX detection | `+DET+UIN`/`+DET+OUT` and `+AUX+UIN`/`+AUX+OUT` update separate detection values. The AUX removal branch calls the mute/audio-control helper when AUX is selected. | MCP can select each source. Physical hot-plug behavior and public notification availability need a dedicated test. |
| Recovery and factory paths | An internal `+BUG+ESD` branch clears control state at `0x0800352E`. OTA has its own display path. | These branches do not establish user-facing diagnostic or maintenance controls. |
| Front panel | The eight-byte buffer can be written raw. The stock firmware also renders numeric overlays, separators, input and EQ indicators. | All working bytes are controllable. A camera-verified map of individual bits, including the second always-on startup group, is still missing. |

The Cortex-M image covers lighting, local controls and coordination. The BES
Bluetooth/audio controller and the GaN charging controller have separate firmware.
The [QD35 manual](https://cdn.ventmere.com/edifier-dev/uploads/2023-03/QD35%20User%20Manual-Online-V1.0-EN_HJb_VEh0i.pdf)
documents USB-A computer audio, AUX, Bluetooth, and charging in standby. Its
charging limits are 35 W on USB-C alone, 18 W on USB-A alone, and 18 W per port
when both are used. The observed BLE capabilities do not advertise charging
telemetry, a prompt setting, or the app's generic light and shutdown timers.

## Confidence and limits

The byte layout, receive-time byte order, three type branches, type-1 timer behavior,
37-slot bound, and type-3 first-color behavior are direct firmware observations.
RGB channel order is also confirmed by the physical RGB-bars recording. The timing
unit combines firmware countdown behavior with the 240 fps hardware recording.

The exact hidden package coordinates on the owner's QD35 remain unmeasured because
the visible acrylic guides move and diffuse each source. The firmware serializer
and the synchronized recording establish the 37-node wire count and the lower-
strip/ring topology without pretending that visible centroids are PCB coordinates.
The domestic NEW-X teardown directly documents the lower board. The synchronized
international-QD35 recording covers the lower guide and mid-bass ring together.
