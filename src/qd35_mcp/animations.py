"""Validated QD35 animation records and hardware-diagnostic patterns."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import ProtocolError

LIGHT_COUNT = 37
FRAME_SIZE = 150
DEVICE_CAPACITY = 15_360
MAX_FRAMES = DEVICE_CAPACITY // FRAME_SIZE
MAX_AUTHORED_DURATION_MS = 10 * 60 * 1000
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,47}$")
_RGB_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")

Color = tuple[int, int, int, int]

# Front-view visible-emission centroids measured from the synchronized address mapper.
# Coordinates are percentages within the transparent lighting chamber, left-to-right and
# top-to-bottom. They describe where the driven address visibly emits through the light
# guides, not the hidden physical LED package.
ADDRESS_EMISSION_MAP: tuple[tuple[float, float], ...] = (
    (9.9, 94.8),
    (19.5, 88.7),
    (22.6, 89.1),
    (25.9, 73.6),
    (35.3, 71.3),
    (37.6, 82.4),
    (37.3, 90.6),
    (40.7, 86.2),
    (43.6, 91.8),
    (47.6, 91.0),
    (51.9, 90.0),
    (62.6, 91.3),
    (68.1, 92.8),
    (69.2, 92.0),
    (70.6, 90.7),
    (75.3, 76.4),
    (77.0, 75.1),
    (85.6, 88.9),
    (88.3, 92.8),
    (95.2, 94.5),
    (67.7, 4.4),
    (88.0, 2.0),
    (91.9, 32.3),
    (74.3, 73.5),
    (89.7, 53.2),
    (75.9, 74.3),
    (67.1, 95.2),
    (69.7, 94.8),
    (47.7, 98.2),
    (26.8, 88.9),
    (13.9, 62.4),
    (28.7, 42.0),
    (31.9, 28.4),
    (41.8, 2.6),
    (51.5, 10.5),
    (62.5, 17.8),
    (64.5, 8.8),
)

# A visually continuous loop through the measured light-guide centroids. Wire order jumps
# from the lower-right run to the top of the chamber at address 20; creative chases use this
# route so motion follows the visible guides instead of that transport discontinuity.
DISPLAY_ROUTE: tuple[int, ...] = (
    0,
    1,
    2,
    29,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    28,
    11,
    12,
    13,
    14,
    26,
    27,
    15,
    16,
    23,
    25,
    17,
    18,
    19,
    24,
    22,
    21,
    20,
    36,
    35,
    34,
    33,
    32,
    31,
    30,
)


@dataclass(frozen=True, slots=True)
class GeneratedAnimation:
    name: str
    data: bytes
    description: str
    observation_request: str
    light_type: int = 1
    asset_id: int = 23
    start_asset_id: int | None = None

    @property
    def frame_count(self) -> int:
        return len(self.data) // FRAME_SIZE

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def nominal_duration_ms(self) -> int:
        return sum(
            int.from_bytes(self.data[offset : offset + 2], "big")
            for offset in range(0, len(self.data), FRAME_SIZE)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "frames": self.frame_count,
            "bytes": len(self.data),
            "sha256": self.sha256,
            "nominal_duration_ms": self.nominal_duration_ms,
            "description": self.description,
            "observation_request": self.observation_request,
            "light_type": self.light_type,
            "asset_id": self.asset_id,
            "start_asset_id": self.start_asset_id,
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


def compile_authored_animation(
    name: str,
    frames: Sequence[tuple[int, Sequence[str]]],
) -> GeneratedAnimation:
    """Compile a bounded local RGB animation without accepting files or URLs."""
    cleaned_name = name.strip()
    if not _NAME_PATTERN.fullmatch(cleaned_name):
        raise ValueError(
            "animation name must be 1-48 characters using letters, numbers, spaces, _ or -"
        )
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError(f"animation must contain between 1 and {MAX_FRAMES} frames")

    encoded: list[bytes] = []
    total_duration = 0
    for frame_index, (duration_ms, colors) in enumerate(frames):
        if not 5 <= duration_ms <= 60_000:
            raise ValueError(
                f"frame {frame_index} duration_ms must be between 5 and 60000"
            )
        if len(colors) != LIGHT_COUNT:
            raise ValueError(f"frame {frame_index} must contain exactly {LIGHT_COUNT} colors")
        lights: list[Color] = []
        for light_index, color in enumerate(colors):
            if not isinstance(color, str) or not _RGB_PATTERN.fullmatch(color):
                raise ValueError(
                    f"frame {frame_index} color {light_index} must be #RRGGBB"
                )
            raw = bytes.fromhex(color[1:])
            lights.append((raw[0], raw[1], raw[2], 0))
        total_duration += duration_ms
        if total_duration > MAX_AUTHORED_DURATION_MS:
            raise ValueError("animation nominal loop duration must not exceed 10 minutes")
        encoded.append(encode_frame(duration_ms, lights))

    animation = GeneratedAnimation(
        cleaned_name,
        b"".join(encoded),
        "User-authored local RGB animation with the fourth component forced to zero.",
        "Verify the physical result; actual frame dwell adds about 3 ms on firmware 1.3.0.",
    )
    validate_animation(animation.data)
    return animation


def _solid(color: Color) -> list[Color]:
    return [color] * LIGHT_COUNT


def _route_frame(route_colors: Sequence[Color]) -> list[Color]:
    if len(route_colors) != LIGHT_COUNT:
        raise ValueError(f"route frame must contain exactly {LIGHT_COUNT} colors")
    lights: list[Color] = [(0, 0, 0, 0)] * LIGHT_COUNT
    for route_index, address in enumerate(DISPLAY_ROUTE):
        lights[address] = route_colors[route_index]
    return lights


def _hsv_color(hue: float, saturation: float = 1, value: float = 1) -> Color:
    # Small local implementation avoids adding a runtime dependency for preset generation.
    hue %= 1
    sector = int(hue * 6)
    fraction = hue * 6 - sector
    p = value * (1 - saturation)
    q = value * (1 - fraction * saturation)
    t = value * (1 - (1 - fraction) * saturation)
    red, green, blue = (
        (value, t, p),
        (q, value, p),
        (p, value, t),
        (p, q, value),
        (t, p, value),
        (value, p, q),
    )[sector % 6]
    return round(red * 255), round(green * 255), round(blue * 255), 0


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


def _address_mapper() -> GeneratedAnimation:
    base = (24, 24, 24, 0)
    markers: tuple[Color, ...] = (
        (255, 0, 0, 0),
        (255, 255, 0, 0),
        (0, 255, 0, 0),
        (0, 255, 255, 0),
        (0, 0, 255, 0),
        (255, 0, 255, 0),
    )
    frames = [
        encode_frame(1_200, _solid((0, 0, 255, 0))),
        encode_frame(300, _solid(base)),
    ]
    for active in range(LIGHT_COUNT):
        lights = [base] * LIGHT_COUNT
        lights[active] = markers[active % len(markers)]
        frames.append(encode_frame(250, lights))
    frames.append(encode_frame(1_200, _solid((255, 255, 0, 0))))
    return GeneratedAnimation(
        "address_mapper",
        b"".join(frames),
        (
            "Blue sync, dim-white baseline, then addresses 0-36 one at a time with a "
            "six-color repeating marker, followed by a yellow end marker."
        ),
        (
            "Record two full blue-to-yellow loops; count from the first colored address after "
            "the dim-white separator to map every physical position."
        ),
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


def _spiral_color_probe() -> GeneratedAnimation:
    colors: tuple[Color, ...] = (
        (255, 0, 0, 0),
        (255, 255, 0, 0),
        (0, 255, 0, 0),
        (0, 255, 255, 0),
        (0, 0, 255, 0),
        (255, 0, 255, 0),
    )
    lights = [colors[index % len(colors)] for index in range(LIGHT_COUNT)]
    return GeneratedAnimation(
        "spiral_color_probe",
        encode_frame(100, lights),
        (
            "One type-3 procedural record with six repeating payload colors; firmware moves "
            "them through the central-speaker ring and lower light guide."
        ),
        (
            "Record how 37 payload colors feed the procedural path; uploaded entries, physical "
            "RGB packages, and visible stopping positions are not one-to-one."
        ),
        light_type=3,
        asset_id=25,
        start_asset_id=1,
    )


def _spiral_index_probe() -> GeneratedAnimation:
    # Encode the payload index as three base-four digits. Every slot therefore has a unique,
    # well-separated RGB value that can be matched back from a color-calibrated recording.
    levels = (48, 117, 186, 255)
    lights = [
        (
            levels[index & 0x03],
            levels[(index >> 2) & 0x03],
            levels[(index >> 4) & 0x03],
            0,
        )
        for index in range(LIGHT_COUNT)
    ]
    return GeneratedAnimation(
        "spiral_index_probe",
        encode_frame(100, lights),
        "A type-3 record in which each of the 37 payload slots has a unique RGB code.",
        (
            "Film the whole front straight-on for at least 15 seconds without moving the camera; "
            "nearest-color matching can reveal which payload slots the procedural path consumes."
        ),
        light_type=3,
        asset_id=25,
        start_asset_id=1,
    )


def _spiral_fourth_probe() -> GeneratedAnimation:
    values = (0, 1, 16, 64, 128, 255)
    lights = [(96, 0, 0, values[index % len(values)]) for index in range(LIGHT_COUNT)]
    return GeneratedAnimation(
        "spiral_fourth_probe",
        encode_frame(100, lights),
        (
            "A type-3 record with constant low red and repeating fourth-component values "
            "0, 1, 16, 64, 128, and 255."
        ),
        (
            "Film the whole path and compare brightness, color, spacing, and motion against the "
            "zero-fourth-byte control; stop if the result flashes."
        ),
        light_type=3,
        asset_id=25,
        start_asset_id=1,
    )


def _spiral_fast_probe() -> GeneratedAnimation:
    return GeneratedAnimation(
        "spiral_fast_probe",
        encode_frame(20, _solid((255, 255, 255, 0))),
        "Official Spiral geometry with its control value changed from 100 to 20.",
        "Compare motion speed with spiral_slow_probe; stop if the result flashes.",
        light_type=3,
        asset_id=25,
        start_asset_id=1,
    )


def _spiral_slow_probe() -> GeneratedAnimation:
    return GeneratedAnimation(
        "spiral_slow_probe",
        encode_frame(400, _solid((255, 255, 255, 0))),
        "Official Spiral geometry with its control value changed from 100 to 400.",
        "Compare motion speed with spiral_fast_probe and the official Spiral.",
        light_type=3,
        asset_id=25,
        start_asset_id=1,
    )


def _pac_man() -> GeneratedAnimation:
    frames: list[bytes] = []
    ghost_colors: tuple[Color, ...] = (
        (255, 24, 24, 0),
        (255, 80, 190, 0),
        (0, 220, 255, 0),
        (255, 126, 28, 0),
    )
    for step in range(LIGHT_COUNT):
        for mouth_open in (True, False):
            route_colors: list[Color] = [(0, 0, 0, 0)] * LIGHT_COUNT
            # Pellets remain ahead of Pac-Man and disappear behind him until the loop resets.
            for distance in range(2, LIGHT_COUNT - 4):
                if distance % 3 == 0:
                    route_colors[(step + distance) % LIGHT_COUNT] = (42, 52, 64, 0)
            route_colors[step] = (255, 224 if mouth_open else 168, 0, 0)
            for ghost_index, distance in enumerate((5, 9, 13, 17)):
                route_colors[(step - distance) % LIGHT_COUNT] = ghost_colors[ghost_index]
            frames.append(encode_frame(90, _route_frame(route_colors)))
    return GeneratedAnimation(
        "pac_man",
        b"".join(frames),
        (
            "Pac-Man follows the measured light-guide loop, eating dim pellets while four "
            "colored ghosts chase behind."
        ),
        "Look for a yellow chomp pulse, disappearing pellets, and red/pink/cyan/orange ghosts.",
    )


def _comet() -> GeneratedAnimation:
    frames: list[bytes] = []
    tail: tuple[Color, ...] = (
        (255, 255, 255, 0),
        (80, 235, 255, 0),
        (24, 130, 255, 0),
        (8, 56, 180, 0),
        (2, 18, 70, 0),
    )
    for step in range(LIGHT_COUNT):
        route_colors: list[Color] = [(0, 0, 2, 0)] * LIGHT_COUNT
        for distance, color in enumerate(tail):
            route_colors[(step - distance) % LIGHT_COUNT] = color
        frames.append(encode_frame(70, _route_frame(route_colors)))
    return GeneratedAnimation(
        "comet",
        b"".join(frames),
        "A bright white/cyan comet with a five-address blue tail follows the visible guide loop.",
        "Confirm that the head follows a continuous path without the wire-order jump at 19→20.",
    )


def _rainbow_wave() -> GeneratedAnimation:
    frames: list[bytes] = []
    frame_count = 60
    for frame_index in range(frame_count):
        route_colors = [
            _hsv_color(route_index / LIGHT_COUNT + frame_index / frame_count, 0.9, 1)
            for route_index in range(LIGHT_COUNT)
        ]
        frames.append(encode_frame(70, _route_frame(route_colors)))
    return GeneratedAnimation(
        "rainbow_wave",
        b"".join(frames),
        "A continuous rainbow rotates around the measured light-guide loop.",
        "Check for smooth color motion and optical blending at adjacent guides.",
    )


def _fireflies() -> GeneratedAnimation:
    frames: list[bytes] = []
    frame_count = 80
    for frame_index in range(frame_count):
        lights: list[Color] = []
        for address in range(LIGHT_COUNT):
            phase = address * 1.731 + frame_index * 0.31
            pulse = max(0.0, math.sin(phase)) ** 8
            shimmer = max(0.0, math.sin(phase * 0.37 + frame_index * 0.09))
            green = round(3 + 210 * pulse)
            red = round(1 + 105 * pulse)
            blue = round(4 + 25 * shimmer)
            lights.append((red, green, blue, 0))
        frames.append(encode_frame(90, lights))
    return GeneratedAnimation(
        "fireflies",
        b"".join(frames),
        "Independent warm-green fireflies pulse asynchronously across all 37 addresses.",
        "Watch for organic twinkles rather than a visible repeating chase.",
    )


def _embers() -> GeneratedAnimation:
    frames: list[bytes] = []
    frame_count = 72
    for frame_index in range(frame_count):
        lights: list[Color] = []
        for address in range(LIGHT_COUNT):
            wave = (
                math.sin(frame_index * 0.23 + address * 1.17)
                + math.sin(frame_index * 0.11 - address * 0.63)
                + 2
            ) / 4
            flare = max(0.0, math.sin(frame_index * 0.41 + address * 2.03)) ** 6
            red = round(80 + 175 * min(1, wave + flare * 0.35))
            green = round(4 + 74 * wave + 54 * flare)
            lights.append((red, green, 0, 0))
        frames.append(encode_frame(100, lights))
    return GeneratedAnimation(
        "embers",
        b"".join(frames),
        "A low, asynchronous red-orange ember bed with occasional brighter flares.",
        (
            "Use low global brightness and verify that the flicker feels irregular rather "
            "than strobed."
        ),
    )


_BUILDERS = {
    "pixel_chase": _pixel_chase,
    "address_mapper": _address_mapper,
    "rgb_bars": _rgb_bars,
    "timing_sweep": _timing_sweep,
    "fourth_byte_sweep": _fourth_byte_sweep,
    "spiral_color_probe": _spiral_color_probe,
    "spiral_index_probe": _spiral_index_probe,
    "spiral_fourth_probe": _spiral_fourth_probe,
    "spiral_fast_probe": _spiral_fast_probe,
    "spiral_slow_probe": _spiral_slow_probe,
}

_CREATIVE_BUILDERS = {
    "pac_man": _pac_man,
    "comet": _comet,
    "rainbow_wave": _rainbow_wave,
    "fireflies": _fireflies,
    "embers": _embers,
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
            "fourth_component": (
                "no observable effect on QD35 firmware 1.3.0; official and authored assets use zero"
            ),
            "control_unit": (
                "milliseconds, plus approximately 3 ms processing overhead per frame on "
                "QD35 firmware 1.3.0"
            ),
        },
        "address_map": {
            "coordinate_system": (
                "front-view percentage within the transparent lighting chamber; "
                "x increases left-to-right and y increases top-to-bottom"
            ),
            "meaning": (
                "video-calibrated visible-emission centroid for each wire address; "
                "not the hidden LED package position"
            ),
            "points": [
                {"address": address, "x_percent": point[0], "y_percent": point[1]}
                for address, point in enumerate(ADDRESS_EMISSION_MAP)
            ],
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


def list_creative_animations() -> dict[str, object]:
    animations = [builder() for builder in _CREATIVE_BUILDERS.values()]
    return {
        "animations": [animation.to_dict() for animation in animations],
        "route": list(DISPLAY_ROUTE),
        "route_basis": "pre-SLAM visible-emission map; independent of experimental 3D poses",
    }


def build_creative_animation(name: str) -> GeneratedAnimation:
    normalized = name.strip().lower().replace("-", "_")
    try:
        animation = _CREATIVE_BUILDERS[normalized]()
    except KeyError as exc:
        choices = ", ".join(_CREATIVE_BUILDERS)
        raise ValueError(f"creative animation must be one of: {choices}") from exc
    validate_animation(animation.data)
    return animation
