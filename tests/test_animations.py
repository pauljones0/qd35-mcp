from __future__ import annotations

import pytest

from qd35_mcp.animations import (
    FRAME_SIZE,
    LIGHT_COUNT,
    build_diagnostic_animation,
    encode_frame,
    list_diagnostic_animations,
    validate_animation,
)
from qd35_mcp.errors import ProtocolError


@pytest.mark.parametrize(
    ("name", "frames"),
    [
        ("pixel_chase", 37),
        ("rgb_bars", 1),
        ("timing_sweep", 8),
        ("fourth_byte_sweep", 6),
    ],
)
def test_diagnostic_animation_geometry(name: str, frames: int) -> None:
    animation = build_diagnostic_animation(name)
    assert animation.frame_count == frames
    assert len(animation.data) == frames * FRAME_SIZE
    assert validate_animation(animation.data) == {
        "frames": frames,
        "bytes": frames * FRAME_SIZE,
    }


def test_pixel_chase_has_exactly_one_active_address_per_frame() -> None:
    animation = build_diagnostic_animation("pixel_chase")
    for frame_index in range(LIGHT_COUNT):
        frame = animation.data[frame_index * FRAME_SIZE : (frame_index + 1) * FRAME_SIZE]
        lights = [frame[offset : offset + 4] for offset in range(2, FRAME_SIZE, 4)]
        assert lights.count(bytes((255, 255, 255, 0))) == 1
        assert lights[frame_index] == bytes((255, 255, 255, 0))


def test_encode_and_validation_fail_closed() -> None:
    with pytest.raises(ValueError, match="exactly 37"):
        encode_frame(20, [(0, 0, 0, 0)])
    with pytest.raises(ProtocolError, match="complete 150-byte"):
        validate_animation(b"x")
    with pytest.raises(ProtocolError, match="live QD35"):
        validate_animation(bytes(FRAME_SIZE * 103))


def test_catalog_describes_unresolved_fields() -> None:
    catalog = list_diagnostic_animations()
    assert catalog["format"]["lights_per_frame"] == 37
    assert catalog["format"]["maximum_complete_frames"] == 102
    assert catalog["format"]["control_unit"] == "unresolved"
