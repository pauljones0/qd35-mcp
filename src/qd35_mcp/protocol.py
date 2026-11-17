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
                # A corrupt but plausible length can otherwise hold a later valid
                # notification forever. Only resynchronize when the later header
                # already forms a complete, checksum-valid frame.
                replacement = self._find_complete_frame_start()
                if replacement is not None:
                    items.append(
                        ProtocolError(
                            f"incomplete frame superseded by a valid frame at byte {replacement}"
                        )
                    )
                    del self._buffer[:replacement]
                    continue
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

    def _find_complete_frame_start(self) -> int | None:
        for offset in range(1, len(self._buffer) - 5):
            if self._buffer[offset] not in (0xBB, 0xCC):
                continue
            if self._buffer[offset + 1] != 0xEC:
                continue
            payload_length = int.from_bytes(self._buffer[offset + 3 : offset + 5], "big")
            if payload_length > self._max_payload:
                continue
            total = payload_length + 6
            candidate = bytes(self._buffer[offset : offset + total])
            if len(candidate) != total:
                continue
            try:
                decode_frame(candidate, self._xor_payload)
            except ProtocolError:
                continue
            return offset
        return None


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
