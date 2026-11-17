# QD35 BLE protocol

## Identity and transport

| Field | Value |
|---|---|
| Edifier catalog product ID | 223 |
| Advertised search UUID | `0000a400-0000-1000-8000-00805f9b34fb` |
| GATT service | `4809a401-1a48-11e9-ab14-d663bd873d93` |
| Device → host read/notify | `48090001-1a48-11e9-ab14-d663bd873d93` |
| Host → device write | `48090002-1a48-11e9-ab14-d663bd873d93` |
| Manufacturer company ID | `0x07E0` (Edifier) |

The live advertisement contained manufacturer bytes ending in `02 00`: framing version 2 and encryption selector 0 (plaintext). Selector `0x10` means XOR each payload byte with `0xA5` before checksum; the codec supports both.

BLE is the control path. Classic Bluetooth A2DP/AVRCP is a separate audio link.

## Frame format

```text
request: AA EC OPCODE LENGTH_BE16 PAYLOAD... CHECKSUM
reply:   BB EC OPCODE LENGTH_BE16 PAYLOAD... CHECKSUM
ack:     CC may replace BB on some firmware/models

CHECKSUM = sum(all preceding wire bytes) & FF
```

The implementation validates the full 16-bit length, checksum, fragmentation, and multiple frames in one notification. It serializes requests because unsolicited events such as media metadata may arrive between a request and its reply. `C2` media control is explicitly one-way; all other implemented setters use request/reply or connection-loss handling.

## Live firmware 1.3.0 observations

Captured from a physical speaker identified by `C9` as `EDIFIER QD35`:

| Query | Response payload | Interpretation |
|---|---|---|
| `66` volume | `10 0A` | maximum 16, current 10 |
| `61` input | `02 01` | group 2, Bluetooth |
| `D5` EQ preset | `00` | Music |
| `43` custom EQ | `03 06 ... 00 00 00 00` | profile 3, six 6-byte band records, then four reserved zero bytes |
| `6A` lighting | `01 04 ...` | group 1, effect 4, seven effect records |
| `48` LDAC | `00` | disabled |
| `C6` firmware | `01 03 00` | 1.3.0 |
| `C8` address | six bytes | Classic audio address |
| `C9` name | UTF-8 | `EDIFIER QD35` |
| `D8` capability block | `0000010401010100000001010a0c0a00002004830401` | 22-byte, length-aware capability block; raw bytes and unknown bits are retained |
| `68` audio status | `00` while idle | raw value plus the app-confirmed QCM, Hi-Res, line Hi-Res, 192 kHz, and HWA logo combination |
| `78` custom light | two bytes | installed Extended asset ID and detail byte; values 1–11 map to palette keys, while other observed values remain raw |

Implemented request/setter pairs. Definitions are app-confirmed. Changed-value volume,
input, EQ preset, EQ band, lighting, LDAC, rename, and clock operations were observed
live with readback/restoration where available. Media and disruptive validation are
tracked separately in `hardware-validation.md`:

| Function | Query | Set/action | Payload |
|---|---:|---:|---|
| Volume | `66` | `67` | current byte |
| Input | `61` | `62` | group `02`, Bluetooth `01`, AUX `02`, USB `03` |
| EQ preset | `D5` | `C4` | Music `00`, Game `01`, Movie `02`, DIY `03` |
| EQ band | `43` | `44` | index, `00`, frequency BE16, gain code, Q |
| EQ reset | — | `45` | empty; query `43` afterward |
| Lighting | `6A` | `6B` | group, effect, three effect-specific color bytes, global brightness, wire speed |
| Lighting reset | — | `72` | group, effect; response parses as lighting state |
| Light timer | `69` | `70` | index, value; gated by D8 byte 19 bit 3 |
| Custom-light state | `78` | — | installed asset ID and optional palette key |
| Custom-light transfer | — | `79`, `7A`, `7B` | start, acknowledged chunks, complete/abort |
| LDAC | `48` | `49` | off `00`, 48 kHz `01`, 96 kHz `02` |
| Clock | — | `77` | time and format, capability-dependent |
| Media | `C3` | `C2` (one-way) | play `00`, pause `01`, next `04`, previous `05` |
| Rename | `C9` | `CA` | UTF-8 name |
| Disconnect | — | `CD` | empty |
| Power off | — | `CE` | empty |
| Factory reset | — | `07` | empty |

The live D8 block decodes to rename mode 4 (35 UTF-8 bytes, no packaged write), EQ profile 12, UI skin 10, and support for name/address/firmware queries, disconnect, shutdown, reset, LDAC, input, volume, ambient lighting, audio-decoding status, and the hour/minute/second clock dialect. It does not advertise the no-audio auto-shutdown, shutdown timer, separate light-color setting, or lighting timer bits. Byte 19 bit 4 is version-sensitive: app 8.4.33 treats set as media-info support, while 8.5.5 treats clear as support; the live clear bit therefore does not mean metadata is absent, and a `50` event was observed. Capability parsing is length-aware, treats byte 4 as the 8.5.5 bit field, and preserves trailing or unknown data instead of assuming a fixed 22-byte format.

QD35 group-1 color bytes are effect-specific. Ambient and magic-tides use first-byte
values `01` light and `02` deep, followed by two zero bytes. Shooting-star, breathing,
tempo-emo, and static use RGB. Extended uses first-byte values `00` off and `01` on,
followed by two zero bytes. Brightness is global: a `6B` change is reflected in all
seven `6A` records. Speed is inverted on the wire (`wire_speed = 11 - UI_speed`); `FF`
means the effect has no speed control.

Playback query `C3` returns one byte. The official app treats `0D` as playing and other
observed values (for example `03`) as not playing. It can verify play/pause state but
does not prove that a next/previous command changed tracks.

For the live hour/minute/second clock dialect, the hour byte remains the host's 0–23 hour for both display formats. Choosing 12-hour display appends format `01` and an AM/PM byte; it does not convert the hour byte to 1–12. Choosing 24-hour display appends format `02`.

## Extended/custom-light transfer

Custom animations are assets inside ordinary effect `07` (Extended), not additional `6B` effect IDs. For QD35 product 223 and firmware 1.3.0, Edifier's catalog provides Gradient (asset 23/type 1), Water-drop (24/type 1), Spiral (25/type 3, but start ID 1 due to the app compatibility patch), and Multicolored (26/type 2).

The transfer sequence is:

```text
78 read current identity
79 <start_asset_id> [palette_key]
   reply: version[3], unresolved byte, transfer_limit_BE16, capacity_BE32
7A TYPE 07 SEQUENCE 00 25 DATA[transfer_limit-5] 00 00
   reply: status 00 advances; otherwise retry the same sequence and offset
7B 01 complete (7B 02 aborts)
78 verify installed identity, then optionally select Extended with 6B
```

The app requests ATT MTU 512 and waits two seconds before the first chunk. On the live QD35, `79` reported limit 155 and capacity 15,360, producing 150-byte application chunks and 163-byte frames. Sequence numbers start at 1. Matching the app, negative replies and lost acknowledgements retry the identical sequence and file offset, bounded at ten attempts. This was verified live by sending the exact same accepted Spiral sequence-1 frame twice: both replies were successful with detail `00`, completion succeeded, and `78` reported the intended asset and palette. The receiver therefore treats a repeated sequence idempotently on tested firmware 1.3.0. The second live acknowledgement byte is the zero-based chunk index (`00` for sequence 1, `01` for sequence 2), so stale acknowledgements cannot advance a later chunk. The implementation refuses to begin unless one ATT write can carry the complete live frame, because fragmented delivery failed on the tested QD35. Each official binary is composed of 150-byte records: a two-byte big-endian duration followed by 37 `[R,G,B,reserved]` light entries. Firmware analysis confirms that type 1 interprets the duration as milliseconds and does not load the reserved byte.

Official downloads use a fixed Edifier HTTPS allowlist, a one-megabyte bound, pinned SHA-256 values, and structural validation before transfer. The palette transform multiplies each of the RGB components by the selected palette component and floors the result after division by 255; the first two bytes and reserved byte are preserved.

The local diagnostic compiler uses the same validated geometry and exposes a 37-address chase, a synchronized address mapper, RGB bars, a timing sweep, and a legacy reserved-byte sweep. The address mapper begins with a 1.2-second all-blue sync marker, uses a dim-white baseline while addresses 0–36 receive a repeating six-color marker for 250 ms each, and ends with a 1.2-second yellow marker. This makes absolute address counting possible even when a recording starts mid-loop. Diagnostics use the live-verified type-1 frame-sequence interpreter and Gradient identity; type 3 is reserved for Spiral's procedural interpreter and is unsuitable for address-order measurements.

The public address map reports front-view visible-emission centroids in chamber-relative percentages. It intentionally does not call those points physical LED-package coordinates: the QD35 routes light through transparent guides, so a single address can illuminate an extended path and its visible centroid changes with viewpoint. The synchronized recording established wire identity; structure-from-motion was used only as a geometry/occlusion cross-check after a denser exact-frame bundle adjustment proved degenerate.

Spiral uses the type-3 procedural interpreter rather than type-1 frame playback. Its official binary is one record: header bytes `00 64` followed by 37 identical `FF FF FF 00` entries. Firmware ignores that header and records 1–36; record 0 supplies white as the procedural effect's color. The moving path is therefore not present in the asset; firmware generates it from the type and effect metadata. At 20% brightness, live observation confirmed the app manual's description: motion runs clockwise around the central-speaker ring and right-to-left along the lower guide. The owner's initial estimate of roughly 11 ring positions was made at 5% brightness and is not treated as a count.

The [domestic NEW-X teardown](https://www.ednchina.com/technews/22905.html) reports 20 RGB packages on its lighting board, but it must not be used as the physical count for the owner's international QD35. QD35 firmware serializes 37 consecutive 24-bit addressable-RGB words, and the synchronized live recording shows a distinct controlled emission for every address: 0–19 run left-to-right through the lower guide and 20–36 run around the mid-bass ring. The exact hidden package coordinates remain unknown without opening that unit. Firmware analysis also resolved the earlier type-3 mapping question: Spiral reads only record 0's RGB and generates both visible paths internally; its duration/header, reserved byte, and records 1–36 are ignored.

For QD35 firmware 1.3.0, the official catalog uses type 1 for literal Gradient/Water-drop frame sequences, type 2 for the audio-reactive Multicolored sequence, and type 3 for Spiral's procedural path. A sweep of all 158 products bundled in Edifier Connect 8.3.43 against both current official foreign and China light-effect catalogs found only type values 1, 2, and 3; the inspected 8.5.5 transfer code still passes the catalog type through as an opaque byte. No app branch, downloadable asset, or catalog entry defines type 4. These values are firmware/model-specific interpreter selectors rather than a portable enum: NEW-X PRO, for example, labels both Spiral and Multicolored as type 3. The MCP therefore refuses to invent or transmit type 4.

Video calibration on firmware 1.3.0 resolved the timing field. A 37-frame chase using control value 100 repeated every about 3.80 seconds. A separate timing sweep captured at 240 fps repeated in 251 captured frames (1.046 seconds) for control values totaling 1,020. The combined evidence fits `frame duration = control value in milliseconds + approximately 3 ms processing overhead`. Firmware analysis then confirmed a big-endian 16-bit millisecond countdown and showed that zero is a non-decrementing hold sentinel. The fourth byte of each light record was swept through 0, 1, 16, 64, 128, and 255 at 640 ms per state. Neither visual inspection nor stabilized luminance/RGB analysis found a repeatable six-state effect. The firmware's type-1 and type-3 renderers never load it; type 2 does not render it but can include it in a halfword occupancy test. It is therefore named `reserved` and forced to zero for authored animations. See [firmware-analysis.md](firmware-analysis.md) for the disassembly evidence.

The authored-animation tool accepts only inline RGB data: 1–102 frames, exactly 37 `#RRGGBB` colors per frame, 5–60,000 ms per frame, and at most ten minutes of nominal loop time. It never accepts a path or URL, forces byte four to zero, validates the 15,360-byte device bound, and uses the same target, firmware, MTU, acknowledgement, abort, and readback checks as official assets.

Creative presets use the same bounded type-1 compiler without file or network input. Pac-Man and comet follow a 37-address route derived from the pre-SLAM front-view emission map so the visible motion avoids wire order's large 19→20 jump. Rainbow wave rotates hue along that route; fireflies and embers use deterministic per-address oscillators so their binaries remain reproducible and hashable. Firmware analysis confirms that type 1 is the correct literal-pixel interpreter. Type 3 ignores payload records 1–36 and generates Spiral's route internally, so it cannot encode Pac-Man geometry.

## Evidence discipline

Protocol constants came from official app definitions and were then checked against live QD35 replies. Related open-source projects informed transport design but were not treated as QD35 proof:

- [edifier-speakers-ble](https://github.com/kralonur/edifier-speakers-ble) (MIT)
- [d32-remote](https://github.com/terezos/d32-remote) (MIT)
- [liberated-edifier-s880db-mkii](https://github.com/MikeBirdTech/liberated-edifier-s880db-mkii) (MIT)
- [hass-edifier-ble-component](https://github.com/j1kwen/hass-edifier-ble-component) (MIT)

No upstream source was copied. Do not treat commands from another Edifier model as QD35-compatible without device readback.

