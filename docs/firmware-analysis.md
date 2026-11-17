# QD35 firmware analysis

This document records independently derived facts from Edifier's official QD35
firmware and the Edifier Connect application. Addresses below use image base
`0x08002000`. They describe QD35 firmware 1.3.0 only unless stated otherwise.

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

This is sufficient to retrieve and inspect the correct image. It is not sufficient
reason to reflash an already-current speaker: rollback, bootloader recovery, power-
loss behavior, and signature/integrity enforcement have not been established.

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

A teardown of the domestic NEW-X reports 20 RGB packages on its lighting PCB. Its
photograph does show 20 packages in a row, so this is not safely interchangeable
with the live international-QD35 count despite the products sharing application
code and animation files. The difference may be a regional or board revision. The
strong statement supported for the tested QD35 is **37 independently controlled
RGB nodes and 37 visible responses**; locating the hidden packages themselves would
require opening that exact unit.

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

`0x08006074` maps ten live audio levels to groups of uploaded records, merges the
selected records into a working frame, and renders the same 37 RGB triples. The first
two bytes are not used as a duration by this path. The reserved byte is not sent to
the pixel writer. Because the merge code tests entire 16-bit halves for zero before
copying them, a nonzero reserved byte can still alter which half-record wins during
the merge even though it is not rendered. It must remain zero.

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

The clock/status glass and the EDIFIER wordmark are driven by a separate CMS1640-
compatible 8-by-8 segment buffer at `0x200016B8`. The bit-banged driver is at
`0x08008E76` through `0x0800900C`:

| Function | Address | Behavior |
|---|---:|---|
| write display RAM | `0x08008F28` | sends data command `0x40`, address/data, then display-on `0x8F` |
| clear all RAM | `0x08008F94` | writes 16 zeros starting at `0xC0` |
| display on | `0x08008FCA` | sends the supplied display-control byte; callers use `0x8F` |
| display off | `0x08008FEE` | sends `0x80` |

The firmware always enables this driver at maximum CMS1640 brightness (`0x8F`). It
never uses the intermediate brightness commands `0x88` through `0x8E`.

Clock digits occupy six grid bits in the eight-byte RAM. `0x0800689A` writes one
numeric digit, `0x08006ACE` renders the live time, and `0x08006B28` can clear the six
digit grids while preserving the remaining grids. The `+TIM+SET` handler at
`0x08003258` only changes hour, minute, second, and 12/24-hour state, commits those
values to the RTC, and redraws the display. There is no clock-enable, clock-
brightness, or clock-off field in that command.

The static wordmark is independently represented inside the segment RAM. During
display initialization, `0x08006A2E(1)` sets grid bit 6 across all eight segment
bytes; the same routine supports clearing it with argument zero. It is called only
once, with argument one, from `0x08006D50`. No parsed Bluetooth/MCU command calls it
again. This is strong firmware evidence that the logo is electrically controllable
but deliberately fixed on by the stock firmware.

The only production path that blanks both the clock and wordmark is speaker standby:
`0x08008382` clears the RGB output, serializes the black frame, and sends CMS1640
display-off `0x80`. Waking the speaker reinitializes the display and turns the
wordmark back on. Standby also takes the speaker out of its normal active Bluetooth
audio state, so it does not satisfy “clock off while Bluetooth stays connected.”

Consequently, stock firmware 1.3.0 supports these combinations:

| Requested state | Stock result |
|---|---|
| RGB off, Bluetooth active | supported |
| Clock synchronized or 12/24-hour format changed | supported |
| Clock off, Bluetooth active | not exposed |
| Logo off, clock on | not exposed |
| Clock/logo brightness adjustment | not exposed; hard-coded maximum |
| All lights and displays off | supported only through speaker standby |

A modified lighting-MCU image could blank the digit grids, clear the wordmark grid,
or send a lower CMS1640 brightness command independently. Doing that dynamically
would also require a safe command path from the BES Bluetooth firmware, and OTA
rollback/recovery is not established. These are firmware-patch possibilities, not
capabilities that the MCP should advertise today.

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

There is no `DISPLAY`, `CLOCK OFF`, `LOGO`, segment-RAM, or display-brightness
command token or parser branch. Searches of Edifier Connect 8.4.33 and 8.5.5 also
found no corresponding QD35 command builder. This rules out a merely hidden app UI
control for the stock protocol; it does not rule out a future custom-firmware path.

## Confidence and limits

The byte layout, receive-time byte order, three type branches, type-1 timer behavior,
37-slot bound, and type-3 first-color behavior are direct firmware observations.
RGB channel order is also confirmed by the physical RGB-bars recording. The timing
unit combines firmware countdown behavior with the 240 fps hardware recording.

The exact hidden package coordinates on the owner's QD35 remain unmeasured because
the visible acrylic guides move and diffuse each source. The firmware serializer
and the synchronized recording establish the 37-node wire count and the lower-
strip/ring topology without pretending that visible centroids are PCB coordinates.
The domestic NEW-X teardown's 20-package board is retained as variant evidence, not
used to override measurements from the international QD35.
