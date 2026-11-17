from __future__ import annotations

import pytest

from qd35_mcp.animations import (
    ADDRESS_EMISSION_MAP,
    FRAME_SIZE,
    LIGHT_COUNT,
    build_diagnostic_animation,
    compile_authored_animation,
    encode_frame,
    list_diagnostic_animations,
    validate_animation,
)
from qd35_mcp.errors import ProtocolError


@pytest.mark.parametrize(
    ("name", "frames"),
    [
        ("pixel_chase", 37),
        ("address_mapper", 40),
        ("rgb_bars", 1),
        ("timing_sweep", 8),
        ("fourth_byte_sweep", 6),
        ("spiral_color_probe", 1),
        ("spiral_fast_probe", 1),
        ("spiral_slow_probe", 1),
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


def test_address_mapper_has_sync_markers_and_indexed_colored_addresses() -> None:
    animation = build_diagnostic_animation("address_mapper")
    assert animation.nominal_duration_ms == 11_950
    assert animation.data[:2] == (1_200).to_bytes(2, "big")
    first_address = animation.data[2 * FRAME_SIZE : 3 * FRAME_SIZE]
    lights = [first_address[offset : offset + 4] for offset in range(2, FRAME_SIZE, 4)]
    assert lights[0] == bytes((255, 0, 0, 0))
    assert lights[1:] == [bytes((24, 24, 24, 0))] * 36
    final = animation.data[-FRAME_SIZE:]
    assert final[:2] == (1_200).to_bytes(2, "big")
    assert set(final[offset : offset + 4] for offset in range(2, FRAME_SIZE, 4)) == {
        bytes((255, 255, 0, 0))
    }


def test_spiral_probes_use_the_procedural_interpreter() -> None:
    color = build_diagnostic_animation("spiral_color_probe")
    assert color.light_type == 3
    assert color.asset_id == 25
    assert color.start_asset_id == 1
    lights = [color.data[offset : offset + 4] for offset in range(2, FRAME_SIZE, 4)]
    assert len(set(lights)) == 6
    assert build_diagnostic_animation("spiral_fast_probe").data[:2] == b"\x00\x14"
    assert build_diagnostic_animation("spiral_slow_probe").data[:2] == b"\x01\x90"


def test_authored_animation_is_bounded_rgb_only() -> None:
    colors = ["#123456"] * LIGHT_COUNT
    animation = compile_authored_animation("My loop", [(250, colors), (500, colors)])
    assert animation.frame_count == 2
    assert animation.nominal_duration_ms == 750
    assert animation.data[2:6] == bytes((0x12, 0x34, 0x56, 0))
    with pytest.raises(ValueError, match="exactly 37"):
        compile_authored_animation("Bad", [(250, colors[:-1])])
    with pytest.raises(ValueError, match="#RRGGBB"):
        compile_authored_animation("Bad", [(250, ["red"] * LIGHT_COUNT)])
    with pytest.raises(ValueError, match="10 minutes"):
        compile_authored_animation("Too long", [(60_000, colors)] * 11)


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
    assert catalog["format"]["control_unit"].startswith("milliseconds")
    assert catalog["format"]["fourth_component"].startswith("no observable effect")
    points = catalog["address_map"]["points"]
    assert len(points) == LIGHT_COUNT == len(ADDRESS_EMISSION_MAP)
    assert [point["address"] for point in points] == list(range(LIGHT_COUNT))
    assert all(0 <= point["x_percent"] <= 100 for point in points)
    assert all(0 <= point["y_percent"] <= 100 for point in points)
    assert "not the hidden LED package" in catalog["address_map"]["meaning"]
