"""Edifier BES v2 frame codec and QD35 payload parsers."""

from __future__ import annotations

from dataclasses import dataclass

from .constants import EQ_PRESET_NAMES, INPUT_NAMES, LDAC_MODE_NAMES, LIGHT_EFFECT_NAMES
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


def parse_volume(payload: bytes) -> dict[str, int]:
    if len(payload) < 2:
        raise ProtocolError("volume response needs maximum and current values")
    return {"maximum": payload[0], "current": payload[1]}


def parse_input(payload: bytes) -> dict[str, int | str]:
    if len(payload) < 2:
        raise ProtocolError("input response needs group and value")
    return {
        "group": payload[0],
        "value": payload[1],
        "name": INPUT_NAMES.get(payload[1], f"unknown_{payload[1]}")
        if payload[0] == 2
        else "unknown",
    }


def parse_eq_preset(payload: bytes) -> dict[str, int | str]:
    if not payload:
        raise ProtocolError("EQ preset response is empty")
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
    bands = []
    for offset in range(2, expected, 6):
        record = payload[offset : offset + 6]
        gain_code = record[4]
        bands.append(
            {
                "index": record[0],
                "frequency_hz": int.from_bytes(record[2:4], "big"),
                "gain_code": gain_code,
                "gain_db": (gain_code - 6) / 2,
                "q": record[5],
            }
        )
    band_ids = [band["index"] for band in bands]
    if sorted(band_ids) != list(range(count)):
        raise ProtocolError(
            f"custom EQ band IDs must be 0..{count - 1}, received {band_ids}"
        )
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
        effects.append(
            {
                "id": record[0],
                "name": LIGHT_EFFECT_NAMES.get(record[0], f"effect_{record[0]}"),
                "rgb": f"#{record[1]:02X}{record[2]:02X}{record[3]:02X}",
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
    if not payload:
        raise ProtocolError("LDAC response is empty")
    return {"value": payload[0], "mode": LDAC_MODE_NAMES.get(payload[0], "unknown")}


def parse_firmware(payload: bytes) -> str:
    return ".".join(str(part) for part in payload)
