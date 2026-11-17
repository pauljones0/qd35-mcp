"""Private display-control protocol implemented by the optional MCU patch."""

from __future__ import annotations

DISPLAY_MAGIC = 0x35
DISPLAY_GROUP = 1
DISPLAY_EFFECT = 0

DISPLAY_SET_STATE = 0xD0
DISPLAY_WRITE_GRID = 0xD1
DISPLAY_RESET = 0xD2

DISPLAY_POWER = 1 << 0
DISPLAY_CLOCK = 1 << 1
DISPLAY_LOGO = 1 << 2
DISPLAY_SYMBOLS = 1 << 3
DISPLAY_FOLLOW_DIAL = 1 << 4
DISPLAY_RAW_MODE = 1 << 5

DISPLAY_DEFAULT_FLAGS = DISPLAY_POWER | DISPLAY_CLOCK | DISPLAY_LOGO | DISPLAY_SYMBOLS


def state_payload(flags: int, brightness: int) -> bytes:
    if not 0 <= flags <= 0x3F:
        raise ValueError("display flags must fit in six bits")
    if not 0 <= brightness <= 7:
        raise ValueError("display brightness must be from 0 to 7")
    return bytes(
        (
            DISPLAY_GROUP,
            DISPLAY_EFFECT,
            DISPLAY_SET_STATE,
            flags,
            brightness,
            0,
            DISPLAY_MAGIC,
        )
    )


def grid_payload(index: int, value: int) -> bytes:
    if not 0 <= index <= 7:
        raise ValueError("display grid index must be from 0 to 7")
    if not 0 <= value <= 255:
        raise ValueError("display grid value must be from 0 to 255")
    return bytes(
        (
            DISPLAY_GROUP,
            DISPLAY_EFFECT,
            DISPLAY_WRITE_GRID,
            index,
            value,
            0,
            DISPLAY_MAGIC,
        )
    )


def reset_payload() -> bytes:
    return bytes(
        (
            DISPLAY_GROUP,
            DISPLAY_EFFECT,
            DISPLAY_RESET,
            0,
            0,
            0,
            DISPLAY_MAGIC,
        )
    )


def describe_flags(flags: int, brightness: int) -> dict[str, object]:
    power = bool(flags & DISPLAY_POWER)
    clock = bool(flags & DISPLAY_CLOCK)
    logo_grid = bool(flags & DISPLAY_LOGO)
    if not power:
        logo_extinction = "verified"
    elif clock:
        logo_extinction = "not_achievable_with_clock_on"
    else:
        logo_extinction = "unverified"
    return {
        "power": power,
        "clock": clock,
        "logo": logo_grid,
        "logo_grid_enabled": logo_grid,
        "visible_logo_extinction": logo_extinction,
        "logo_optical_coupling": (
            "The EDIFIER wordmark remains visibly illuminated whenever the clock digits "
            "are lit, even with every non-digit driver bit zero. Whole-display off is the "
            "only verified full-extinction state."
        ),
        "symbols": bool(flags & DISPLAY_SYMBOLS),
        "follow_lighting_dial": bool(flags & DISPLAY_FOLLOW_DIAL),
        "raw_mode": bool(flags & DISPLAY_RAW_MODE),
        "brightness": brightness,
        "brightness_range": [0, 7],
    }
