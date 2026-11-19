"""Private display-control protocol implemented by the optional MCU patch."""

from __future__ import annotations

DISPLAY_MAGIC = 0x35
DISPLAY_GROUP = 1
DISPLAY_EFFECT = 0

DISPLAY_SET_STATE = 0xD0
DISPLAY_WRITE_GRID = 0xD1
DISPLAY_RESET = 0xD2
DISPLAY_UPDATE_STATE = 0xD3
DISPLAY_UPDATE_BRIGHTNESS = 1 << 6

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


def state_update_payload(flags: int, brightness: int, mask: int) -> bytes:
    """Change selected settings on-device without relying on a host cache."""
    if not 1 <= mask <= 0x7F:
        raise ValueError("display update mask must select at least one field")
    payload = bytearray(state_payload(flags, brightness))
    payload[2] = DISPLAY_UPDATE_STATE
    payload[5] = mask
    return bytes(payload)


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
    return {
        "power": power,
        "clock": clock,
        "line": logo_grid,
        "logo": logo_grid,
        "logo_grid_enabled": logo_grid,
        "visible_logo_extinction": "unverified",
        "logo_optical_coupling": (
            "The logo field masks the clock-area line. The physical wordmark stayed lit "
            "with all eight exposed segment bytes zeroed and went dark with display power off."
        ),
        "symbols": bool(flags & DISPLAY_SYMBOLS),
        "follow_lighting_dial": bool(flags & DISPLAY_FOLLOW_DIAL),
        "raw_mode": bool(flags & DISPLAY_RAW_MODE),
        "brightness": brightness,
        "brightness_range": [0, 7],
    }
