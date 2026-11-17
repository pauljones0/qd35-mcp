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

The implementation validates the full 16-bit length, checksum, fragmentation, and multiple frames in one notification. It serializes requests because unsolicited events such as media metadata may arrive between a request and its reply.

## Live firmware 1.3.0 observations

Captured from a physical speaker identified by `C9` as `EDIFIER QD35`:

| Query | Response payload | Interpretation |
|---|---|---|
| `66` volume | `10 0A` | maximum 16, current 10 |
| `61` input | `02 01` | group 2, Bluetooth |
| `D5` EQ preset | `00` | Music |
| `43` custom EQ | `03 06 ...` | profile 3, six 6-byte band records |
| `6A` lighting | `01 04 ...` | group 1, effect 4, seven effect records |
| `48` LDAC | `00` | disabled |
| `C6` firmware | `01 03 00` | 1.3.0 |
| `C8` address | six bytes | Classic audio address |
| `C9` name | UTF-8 | `EDIFIER QD35` |
| `D8` capability block | 22 bytes | raw value retained in `qd35_get_capabilities` |

Implemented request/setter pairs. Definitions are app-confirmed; the same-value volume, input, EQ, and lighting roundtrips were observed live, while changed-value and disruptive validation is tracked separately in `hardware-validation.md`:

| Function | Query | Set/action | Payload |
|---|---:|---:|---|
| Volume | `66` | `67` | current byte |
| Input | `61` | `62` | group `02`, Bluetooth `01`, AUX `02`, USB `03` |
| EQ preset | `D5` | `C4` | Music `00`, Game `01`, Movie `02`, DIY `03` |
| EQ band | `43` | `44` | index, `00`, frequency BE16, gain code, Q |
| Lighting | `6A` | `6B` | group, effect, R, G, B, brightness, wire speed |
| LDAC | `48` | `49` | off `00`, 48 kHz `01`, 96 kHz `02` |
| Clock | — | `77` | time and format, capability-dependent |
| Media | `C3` | `C2` | play `00`, pause `01`, next `04`, previous `05` |
| Rename | `C9` | `CA` | UTF-8 name |
| Disconnect | — | `CD` | empty |
| Power off | — | `CE` | empty |
| Factory reset | — | `07` | empty |

QD35 lighting speed is inverted on the wire: `wire_speed = 11 - UI_speed`. A wire value of `FF` means that effect has no speed control.

## Evidence discipline

Protocol constants came from official app definitions and were then checked against live QD35 replies. Related open-source projects informed transport design but were not treated as QD35 proof:

- [edifier-speakers-ble](https://github.com/kralonur/edifier-speakers-ble) (MIT)
- [d32-remote](https://github.com/terezos/d32-remote) (MIT)
- [liberated-edifier-s880db-mkii](https://github.com/MikeBirdTech/liberated-edifier-s880db-mkii) (MIT)
- [hass-edifier-ble-component](https://github.com/j1kwen/hass-edifier-ble-component) (MIT)

No upstream source was copied. Do not treat commands from another Edifier model as QD35-compatible without device readback.

