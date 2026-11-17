"""Validated QD35 animation records and hardware-diagnostic patterns."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .errors import ProtocolError

LIGHT_COUNT = 37
FRAME_SIZE = 150
DEVICE_CAPACITY = 15_360
MAX_FRAMES = DEVICE_CAPACITY // FRAME_SIZE

Color = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class GeneratedAnimation:
    name: str
    data: bytes
    description: str
    observation_request: str

    @property
    def frame_count(self) -> int:
        return len(self.data) // FRAME_SIZE

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "frames": self.frame_count,
            "bytes": len(self.data),
            "sha256": self.sha256,
            "description": self.description,
            "observation_request": self.observation_request,
        }


def encode_frame(control: int, lights: list[Color]) -> bytes:
    """Encode one recovered 150-byte record without guessing control semantics."""
    if not 0 <= control <= 0xFFFF:
        raise ValueError("animation control value must be between 0 and 65535")
    if len(lights) != LIGHT_COUNT:
        raise ValueError(f"animation frame must contain exactly {LIGHT_COUNT} lights")
    output = bytearray(control.to_bytes(2, "big"))
    for light in lights:
        if len(light) != 4 or any(not 0 <= component <= 255 for component in light):
            raise ValueError("each light must contain four byte-sized components")
        output.extend(light)
    if len(output) != FRAME_SIZE:
        raise AssertionError("internal animation frame geometry is incorrect")
    return bytes(output)


def validate_animation(data: bytes) -> dict[str, int]:
    if not data or len(data) % FRAME_SIZE:
        raise ProtocolError("animation must contain complete 150-byte records")
    if len(data) > DEVICE_CAPACITY:
        raise ProtocolError(
            f"animation is {len(data)} bytes but the live QD35 reports {DEVICE_CAPACITY} bytes"
        )
    return {"frames": len(data) // FRAME_SIZE, "bytes": len(data)}


def _solid(color: Color) -> list[Color]:
    return [color] * LIGHT_COUNT


def _pixel_chase() -> GeneratedAnimation:
    frames: list[bytes] = []
    for active in range(LIGHT_COUNT):
        lights = [(0, 0, 0, 0)] * LIGHT_COUNT
        lights[active] = (255, 255, 255, 0)
        frames.append(encode_frame(0x0064, lights))
    return GeneratedAnimation(
        "pixel_chase",
        b"".join(frames),
        "One white address at a time, in wire order, at a 100-unit official control value.",
        "Record whether the light moves continuously, where index 0 starts, and its direction.",
    )


def _rgb_bars() -> GeneratedAnimation:
    colors: list[Color] = []
    for index in range(LIGHT_COUNT):
        if index < 12:
            colors.append((255, 0, 0, 0))
        elif index < 24:
            colors.append((0, 255, 0, 0))
        else:
            colors.append((0, 0, 255, 0))
    return GeneratedAnimation(
        "rgb_bars",
        encode_frame(0x0280, colors),
        "Twelve red, twelve green, and thirteen blue addresses in one record.",
        "Record the physical grouping, channel correctness, and whether colors interpolate.",
    )


def _timing_sweep() -> GeneratedAnimation:
    controls = (0x0005, 0x000A, 0x0014, 0x0028, 0x0050, 0x0064, 0x007D, 0x0280)
    markers: tuple[Color, ...] = (
        (32, 0, 0, 0),
        (64, 0, 0, 0),
        (96, 0, 0, 0),
        (128, 0, 0, 0),
        (160, 0, 0, 0),
        (192, 0, 0, 0),
        (224, 0, 0, 0),
        (255, 0, 0, 0),
    )
    return GeneratedAnimation(
        "timing_sweep",
        b"".join(
            encode_frame(control, _solid(color))
            for control, color in zip(controls, markers, strict=True)
        ),
        "Eight increasing recovered control values, marked by increasing red intensity.",
        "Record the full loop and measure how long each intensity level remains visible.",
    )


def _fourth_byte_sweep() -> GeneratedAnimation:
    values = (0, 1, 16, 64, 128, 255)
    return GeneratedAnimation(
        "fourth_byte_sweep",
        b"".join(encode_frame(0x0280, _solid((16, 0, 0, value))) for value in values),
        "Low-red frames with the unresolved fourth component stepped from 0 through 255.",
        "Record any brightness, white-channel, color, or transition change; stop if it flashes.",
    )


_BUILDERS = {
    "pixel_chase": _pixel_chase,
    "rgb_bars": _rgb_bars,
    "timing_sweep": _timing_sweep,
    "fourth_byte_sweep": _fourth_byte_sweep,
}


def list_diagnostic_animations() -> dict[str, object]:
    animations = [builder() for builder in _BUILDERS.values()]
    return {
        "animations": [animation.to_dict() for animation in animations],
        "format": {
            "lights_per_frame": LIGHT_COUNT,
            "bytes_per_frame": FRAME_SIZE,
            "device_capacity": DEVICE_CAPACITY,
            "maximum_complete_frames": MAX_FRAMES,
            "fourth_component": "unresolved; official assets use zero",
            "control_unit": "unresolved",
        },
    }


def build_diagnostic_animation(name: str) -> GeneratedAnimation:
    normalized = name.strip().lower().replace("-", "_")
    try:
        animation = _BUILDERS[normalized]()
    except KeyError as exc:
        raise ValueError(f"diagnostic animation must be one of: {', '.join(_BUILDERS)}") from exc
    validate_animation(animation.data)
    return animation
