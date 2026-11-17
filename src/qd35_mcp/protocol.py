"""Edifier BES v2 frame codec and QD35 payload parsers."""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    EQ_PRESET_NAMES,
    INPUT_NAMES,
    LDAC_MODE_NAMES,
    LIGHT_COLOR_MODES,
    LIGHT_EFFECT_NAMES,
    LIGHT_RGB_EFFECT_IDS,
)
from .errors import ProtocolError


@dataclass(frozen=True, slots=True)
class Frame:
    prefix: int
    opcode: int
    payload: bytes


def encode_frame(opcode: int, payload: bytes = b"", xor_payload: bool = False) -> bytes:
    if not 0 <= opcode <= 0xFF:
        raise ValueError("opcode must fit in one byte")
    if len(payload) > 0xFFFF:
        raise ValueError("payload is too large")
    wire_payload = bytes(value ^ 0xA5 for value in payload) if xor_payload else payload
    frame = bytearray((0xAA, 0xEC, opcode, len(payload) >> 8, len(payload) & 0xFF))
    frame.extend(wire_payload)
    frame.append(sum(frame) & 0xFF)
    return bytes(frame)


def decode_frame(data: bytes, xor_payload: bool = False) -> Frame:
    if len(data) < 6:
        raise ProtocolError("frame is shorter than six bytes")
    if data[0] not in (0xAA, 0xBB, 0xCC) or data[1] != 0xEC:
        raise ProtocolError("unexpected frame header")
    payload_length = int.from_bytes(data[3:5], "big")
    expected = payload_length + 6
    if len(data) != expected:
        raise ProtocolError(f"frame length is {len(data)}, expected {expected}")
    if sum(data[:-1]) & 0xFF != data[-1]:
        raise ProtocolError("checksum mismatch")
    payload = data[5:-1]
    if xor_payload:
        payload = bytes(value ^ 0xA5 for value in payload)
    return Frame(prefix=data[0], opcode=data[2], payload=payload)


class FrameStreamDecoder:
    """Buffers fragmented notifications and emits validated frames."""

    def __init__(self, xor_payload: bool = False, max_payload: int = 4096) -> None:
        self._buffer = bytearray()
        self._xor_payload = xor_payload
        self._max_payload = max_payload

    def feed(self, data: bytes) -> list[Frame | ProtocolError]:
        self._buffer.extend(data)
        items: list[Frame | ProtocolError] = []
        while True:
            while self._buffer and self._buffer[0] not in (0xBB, 0xCC):
                del self._buffer[0]
            if len(self._buffer) < 5:
                break
            if self._buffer[1] != 0xEC:
                del self._buffer[0]
                continue
            payload_length = int.from_bytes(self._buffer[3:5], "big")
            if payload_length > self._max_payload:
                items.append(
                    ProtocolError(
                        f"declared payload {payload_length} exceeds {self._max_payload} bytes"
                    )
                )
                del self._buffer[0]
                continue
            total = payload_length + 6
            if len(self._buffer) < total:
                # The payload can legitimately contain a complete frame-shaped byte
                # sequence. Preserve plausible fragmentation until the caller reaches
                # an explicit recovery boundary (timeout/reconnect/reset).
                break
            raw = bytes(self._buffer[:total])
            try:
                items.append(decode_frame(raw, self._xor_payload))
                del self._buffer[:total]
            except ProtocolError as exc:
                items.append(exc)
                # Advance one byte and resynchronize instead of losing a valid frame
                # that may follow a corrupt length/checksum.
                del self._buffer[0]
        return items

    def reset(self) -> None:
        """Discard an incomplete stream only at a known session/recovery boundary."""
        self._buffer.clear()


def parse_volume(payload: bytes) -> dict[str, int]:
    if len(payload) != 2:
        raise ProtocolError(f"volume response needs exactly 2 bytes, received {len(payload)}")
    if payload[0] == 0 or payload[1] > payload[0]:
        raise ProtocolError(f"invalid volume range: maximum {payload[0]}, current {payload[1]}")
    return {"maximum": payload[0], "current": payload[1]}


def parse_input(payload: bytes) -> dict[str, int | str]:
    if len(payload) != 2:
        raise ProtocolError(f"input response needs exactly 2 bytes, received {len(payload)}")
    return {
        "group": payload[0],
        "value": payload[1],
        "name": INPUT_NAMES.get(payload[1], f"unknown_{payload[1]}")
        if payload[0] == 2
        else "unknown",
    }


def parse_eq_preset(payload: bytes) -> dict[str, int | str]:
    if len(payload) != 1:
        raise ProtocolError(f"EQ preset response needs exactly 1 byte, received {len(payload)}")
    return {"value": payload[0], "name": EQ_PRESET_NAMES.get(payload[0], "unknown")}


def parse_eq(payload: bytes) -> dict[str, object]:
    if len(payload) < 2:
        raise ProtocolError("custom EQ response is too short")
    preset, count = payload[:2]
    expected = 2 + count * 6
    if len(payload) < expected:
        raise ProtocolError(
            f"custom EQ declares {count} bands ({expected} bytes), received {len(payload)}"
        )
    # Firmware 1.3.0 appends four reserved zero bytes to the six-band reply.
    # Accept that observed padding, but reject any other undeclared data.
    trailing = payload[expected:]
    if trailing not in (b"", b"\x00\x00\x00\x00"):
        raise ProtocolError(
            f"custom EQ declares {count} bands ({expected} bytes) but has trailing data"
        )
    bands = []
    for offset in range(2, expected, 6):
        record = payload[offset : offset + 6]
        gain_code = record[4]
        frequency = int.from_bytes(record[2:4], "big")
        if frequency == 0:
            raise ProtocolError("custom EQ contains a zero-frequency band")
        if gain_code > 12:
            raise ProtocolError(f"custom EQ gain code {gain_code} exceeds QD35 range 0..12")
        bands.append(
            {
                "index": record[0],
                "frequency_hz": frequency,
                "gain_code": gain_code,
                "gain_db": (gain_code - 6) / 2,
                "q": record[5],
            }
        )
    band_ids = [band["index"] for band in bands]
    if sorted(band_ids) != list(range(count)):
        raise ProtocolError(f"custom EQ band IDs must be 0..{count - 1}, received {band_ids}")
    frequencies = [band["frequency_hz"] for band in bands]
    if len(frequencies) != len(set(frequencies)):
        raise ProtocolError("custom EQ contains duplicate frequencies")
    bands.sort(key=lambda band: band["index"])
    return {"profile": preset, "band_count": count, "bands": bands}


def parse_light(payload: bytes) -> dict[str, object]:
    if len(payload) < 2:
        raise ProtocolError("lighting response is too short")
    if (len(payload) - 2) % 6:
        raise ProtocolError("lighting response contains a truncated effect record")
    group, selected = payload[:2]
    effects = []
    for offset in range(2, len(payload), 6):
        record = payload[offset : offset + 6]
        wire_speed = record[5]
        if record[4] > 100:
            raise ProtocolError(f"lighting brightness {record[4]} exceeds 100")
        if wire_speed != 0xFF and not 1 <= wire_speed <= 10:
            raise ProtocolError(f"lighting wire speed {wire_speed} is not 1..10 or 255")
        effect_id = record[0]
        raw_color = list(record[1:4])
        mode_by_value = {
            value: name for name, value in LIGHT_COLOR_MODES.get(effect_id, {}).items()
        }
        effects.append(
            {
                "id": effect_id,
                "name": LIGHT_EFFECT_NAMES.get(effect_id, f"effect_{effect_id}"),
                "color_control": "rgb" if effect_id in LIGHT_RGB_EFFECT_IDS else "mode",
                "rgb": (
                    f"#{record[1]:02X}{record[2]:02X}{record[3]:02X}"
                    if effect_id in LIGHT_RGB_EFFECT_IDS
                    else None
                ),
                "color_mode": (
                    mode_by_value.get(record[1], f"unknown_{record[1]}")
                    if effect_id in LIGHT_COLOR_MODES and record[2:4] == b"\x00\x00"
                    else None
                ),
                "raw_color": raw_color,
                "brightness": record[4],
                "speed": 11 - wire_speed if 1 <= wire_speed <= 10 else None,
                "wire_speed": wire_speed,
            }
        )
    effect_ids = [effect["id"] for effect in effects]
    if len(effect_ids) != len(set(effect_ids)):
        raise ProtocolError("lighting response contains duplicate effect IDs")
    if selected not in effect_ids:
        raise ProtocolError(f"selected lighting effect {selected} has no state record")
    return {
        "group": group,
        "selected_effect_id": selected,
        "selected_effect": LIGHT_EFFECT_NAMES.get(selected, f"effect_{selected}"),
        "effects": effects,
    }


def parse_light_setting(payload: bytes) -> dict[str, object]:
    if len(payload) != 7:
        raise ProtocolError(
            f"lighting-setting response needs exactly 7 bytes, received {len(payload)}"
        )
    group, effect_id, red, green, blue, brightness, wire_speed = payload
    if brightness > 100:
        raise ProtocolError(f"lighting brightness {brightness} exceeds 100")
    if wire_speed != 0xFF and not 1 <= wire_speed <= 10:
        raise ProtocolError(f"lighting wire speed {wire_speed} is not 1..10 or 255")
    return {
        "group": group,
        "effect_id": effect_id,
        "effect": LIGHT_EFFECT_NAMES.get(effect_id, f"effect_{effect_id}"),
        "raw_color": [red, green, blue],
        "brightness": brightness,
        "speed": 11 - wire_speed if 1 <= wire_speed <= 10 else None,
        "wire_speed": wire_speed,
    }


def parse_ldac(payload: bytes) -> dict[str, int | str]:
    if len(payload) != 1:
        raise ProtocolError(f"LDAC response needs exactly 1 byte, received {len(payload)}")
    return {"value": payload[0], "mode": LDAC_MODE_NAMES.get(payload[0], "unknown")}


def parse_playback(payload: bytes) -> dict[str, int | bool]:
    if len(payload) != 1:
        raise ProtocolError(f"playback response needs exactly 1 byte, received {len(payload)}")
    return {"value": payload[0], "playing": payload[0] == 0x0D}


def parse_firmware(payload: bytes) -> str:
    if not payload:
        raise ProtocolError("firmware response is empty")
    return ".".join(str(part) for part in payload)


def parse_address(payload: bytes) -> str:
    if len(payload) != 6:
        raise ProtocolError(f"Bluetooth address response needs 6 bytes, received {len(payload)}")
    return ":".join(f"{part:02X}" for part in payload)


def parse_audio_status(payload: bytes) -> dict[str, object]:
    if len(payload) != 1:
        raise ProtocolError(
            f"audio decoding response needs exactly 1 byte, received {len(payload)}"
        )
    logo_map = {
        0: [],
        1: ["qcm"],
        4: ["hi_res"],
        5: ["line_hi_res"],
        6: ["192khz"],
        9: ["192khz", "hi_res"],
        10: ["qcm", "hi_res"],
        11: ["hwa"],
        12: ["hwa", "hi_res"],
        13: ["hi_res", "line_hi_res"],
    }
    value = payload[0]
    return {
        "value": value,
        "logos": logo_map.get(value, []),
        "known": value in logo_map,
    }


def parse_custom_light_status(payload: bytes) -> dict[str, int | None]:
    if len(payload) not in (1, 2):
        raise ProtocolError(
            f"custom-light response needs 1 or 2 bytes, received {len(payload)}"
        )
    detail = payload[1] if len(payload) == 2 else None
    return {
        "asset_id": payload[0],
        "detail": detail,
        "palette_key": detail if detail is not None and 1 <= detail <= 11 else None,
    }


def parse_light_timer(payload: bytes) -> dict[str, int]:
    if len(payload) != 2:
        raise ProtocolError(f"light-timer response needs exactly 2 bytes, received {len(payload)}")
    return {"index": payload[0], "value": payload[1]}


def parse_media_info(payload: bytes) -> dict[str, object]:
    if len(payload) == 1:
        return {
            "playing": payload[0] == 1,
            "title": None,
            "artist": None,
            "metadata_present": False,
        }
    if len(payload) < 3:
        raise ProtocolError("media-info response must contain 1 byte or at least 3 bytes")
    title_length, artist_length = payload[1], payload[2]
    expected = 3 + title_length + artist_length
    if len(payload) != expected:
        raise ProtocolError(
            f"media-info declares {expected} bytes, received {len(payload)}"
        )
    title_end = 3 + title_length
    try:
        title = payload[3:title_end].decode("utf-8")
        artist = payload[title_end:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("media-info contains invalid UTF-8") from exc
    return {
        "playing": payload[0] == 1,
        "title": title,
        "artist": artist,
        "metadata_present": True,
    }


def parse_text_event(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("media text event contains invalid UTF-8") from exc


def parse_custom_light_start(payload: bytes) -> dict[str, object]:
    if len(payload) != 10:
        raise ProtocolError(
            f"custom-light start response needs exactly 10 bytes, received {len(payload)}"
        )
    return {
        "version": ".".join(str(value) for value in payload[:3]),
        "unresolved_field": payload[3],
        "transfer_limit": int.from_bytes(payload[4:6], "big"),
        "device_size": int.from_bytes(payload[6:10], "big"),
    }


def parse_custom_light_progress(payload: bytes) -> dict[str, object]:
    if len(payload) != 2:
        raise ProtocolError(
            f"custom-light progress response needs exactly 2 bytes, received {len(payload)}"
        )
    return {"success": payload[0] == 0, "status": payload[0], "detail": payload[1]}


def parse_custom_light_finish(payload: bytes) -> dict[str, object]:
    if len(payload) != 1:
        raise ProtocolError(
            f"custom-light finish response needs exactly 1 byte, received {len(payload)}"
        )
    return {"success": payload[0] == 1, "status": payload[0]}


def _d8_byte(payload: bytes, index: int) -> int:
    return payload[index] if index < len(payload) else 0


def parse_capabilities(payload: bytes) -> dict[str, object]:
    """Decode the length-dependent D8 capability block used by Edifier Connect."""
    if not payload:
        raise ProtocolError("capability response is empty")
    rename_mode = _d8_byte(payload, 3)
    rename_limits = {1: 24, 2: 30, 3: 29, 4: 35, 5: 30, 6: 24}
    b12 = _d8_byte(payload, 12)
    b17 = _d8_byte(payload, 17)
    b18 = _d8_byte(payload, 18)
    b19 = _d8_byte(payload, 19)
    b20 = _d8_byte(payload, 20)
    b21 = _d8_byte(payload, 21)
    supported = {
        "query_name": _d8_byte(payload, 2) == 1,
        "rename": rename_mode in rename_limits,
        "query_address": bool(_d8_byte(payload, 4) & 1),
        "query_firmware": _d8_byte(payload, 5) != 0,
        "disconnect_audio": _d8_byte(payload, 6) != 0,
        "no_audio_auto_shutdown": _d8_byte(payload, 8) != 0,
        "shutdown_timer": _d8_byte(payload, 9) != 0,
        "power_off": _d8_byte(payload, 10) != 0,
        "manual": _d8_byte(payload, 11) != 0,
        "factory_reset": bool(b17 & (1 << 5)),
        "ldac": bool(b18 & (1 << 2)),
        "separate_light_color": bool(b18 & (1 << 7)),
        "input": bool(b19 & (1 << 0)),
        "volume": bool(b19 & (1 << 1)),
        "media_info": None,
        "lighting": bool(b19 & (1 << 7)),
        "hi_res_status": bool(b20 & (1 << 2)),
        "clock_unix_seconds": bool(b20 & (1 << 5)),
        "clock_hms_format": bool(b21 & (1 << 0)),
        "light_timer": bool(b19 & (1 << 3)),
        "smart_light": bool(b21 & (1 << 4)),
    }
    return {
        "raw_hex": payload.hex(),
        "length": len(payload),
        "supported": supported,
        "rename": {
            "mode": rename_mode,
            "max_utf8_bytes": rename_limits.get(rename_mode),
            "packaged_write": 0 < rename_mode < 4,
            "package_size": 20 if 0 < rename_mode < 4 else None,
            "reboots_bluetooth": rename_mode != 6 if rename_mode else None,
        },
        "ota": {
            "android_spp": bool(b12 & 1),
            "android_ble": bool(b12 & 2),
            "ios_spp": bool(b12 & 4),
            "ios_ble": bool(b12 & 8),
        },
        "eq_profile": _d8_byte(payload, 13),
        "skin_profile": _d8_byte(payload, 14),
        "unknown_trailing_hex": payload[22:].hex() if len(payload) > 22 else "",
        "media_info_flag": {
            "bit_set": bool(b19 & (1 << 4)),
            "app_8_4_supports_when": "set",
            "app_8_5_supports_when": "clear",
        },
    }
