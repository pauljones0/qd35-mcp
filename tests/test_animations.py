from __future__ import annotations

import pytest

from qd35_mcp.animations import (
    ADDRESS_EMISSION_MAP,
    DISPLAY_ROUTE,
    FRAME_SIZE,
    LIGHT_COUNT,
    build_creative_animation,
    build_diagnostic_animation,
    compile_authored_animation,
    compile_reactive_animation,
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
        ("spiral_index_probe", 1),
        ("spiral_fourth_probe", 1),
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
    indexed = build_diagnostic_animation("spiral_index_probe")
    indexed_lights = [indexed.data[offset : offset + 4] for offset in range(2, FRAME_SIZE, 4)]
    assert len(set(indexed_lights)) == LIGHT_COUNT
    assert all(light[3] == 0 for light in indexed_lights)
    fourth = build_diagnostic_animation("spiral_fourth_probe")
    fourth_lights = [fourth.data[offset : offset + 4] for offset in range(2, FRAME_SIZE, 4)]
    assert {light[:3] for light in fourth_lights} == {bytes((96, 0, 0))}
    assert {light[3] for light in fourth_lights} == {0, 1, 16, 64, 128, 255}
    assert build_diagnostic_animation("spiral_fast_probe").data[:2] == b"\x00\x14"
    assert build_diagnostic_animation("spiral_slow_probe").data[:2] == b"\x01\x90"


@pytest.mark.parametrize(
    ("name", "frames", "duration_ms"),
    [
        ("pac_man", 74, 6_660),
        ("comet", 37, 2_590),
        ("rainbow_wave", 60, 4_200),
        ("fireflies", 80, 7_200),
        ("embers", 72, 7_200),
    ],
)
def test_creative_animation_geometry(name: str, frames: int, duration_ms: int) -> None:
    animation = build_creative_animation(name)
    assert animation.frame_count == frames
    assert animation.nominal_duration_ms == duration_ms
    assert len(animation.data) <= 15_360
    assert animation.light_type == 1


def test_creative_route_covers_every_address_once() -> None:
    assert len(DISPLAY_ROUTE) == LIGHT_COUNT
    assert sorted(DISPLAY_ROUTE) == list(range(LIGHT_COUNT))


def test_pac_man_has_head_pellets_and_four_distinct_ghosts() -> None:
    animation = build_creative_animation("pac_man")
    first = animation.data[:FRAME_SIZE]
    lights = [first[offset : offset + 4] for offset in range(2, FRAME_SIZE, 4)]
    assert lights[DISPLAY_ROUTE[0]] == bytes((255, 224, 0, 0))
    ghosts = {
        bytes((255, 24, 24, 0)),
        bytes((255, 80, 190, 0)),
        bytes((0, 220, 255, 0)),
        bytes((255, 126, 28, 0)),
    }
    assert ghosts.issubset(set(lights))


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


def test_reactive_animation_preserves_band_and_level_order() -> None:
    bands = [
        [[f"#{band:02x}{level:02x}00"] * LIGHT_COUNT for level in range(10)]
        for band in range(10)
    ]
    animation = compile_reactive_animation("Band test", bands)
    assert animation.light_type == 2
    assert animation.asset_id == 26
    assert animation.frame_count == 100
    assert animation.nominal_duration_ms is None
    assert len(animation.data) == 15_000
    assert animation.data[37 * FRAME_SIZE : 37 * FRAME_SIZE + 6] == b"\x00\x64\x03\x07\x00\x00"
    with pytest.raises(ValueError, match="exactly 10 bands"):
        compile_reactive_animation("Bad", bands[:-1])
    with pytest.raises(ValueError, match="exactly 10 levels"):
        compile_reactive_animation("Bad", [bands[0][:-1], *bands[1:]])


def test_spectrum_meter_uses_disjoint_light_groups() -> None:
    animation = build_creative_animation("spectrum_meter")
    assert animation.light_type == 2
    lights = []
    for band in range(10):
        offset = (band * 10 + 9) * FRAME_SIZE + 2
        lights.extend(
            index for index in range(LIGHT_COUNT)
            if any(animation.data[offset + index * 4 : offset + index * 4 + 3])
        )
    assert sorted(lights) == list(range(LIGHT_COUNT))


def test_catalog_describes_recovered_format() -> None:
    catalog = list_diagnostic_animations()
    assert catalog["format"]["lights_per_frame"] == 37
    assert catalog["format"]["maximum_complete_frames"] == 102
    assert catalog["format"]["control_unit"].startswith("milliseconds")
    assert catalog["format"]["reserved_byte"].startswith("not rendered")
    assert catalog["format"]["fourth_component"].startswith("deprecated name")
    points = catalog["address_map"]["points"]
    assert len(points) == LIGHT_COUNT == len(ADDRESS_EMISSION_MAP)
    assert [point["address"] for point in points] == list(range(LIGHT_COUNT))
    assert all(0 <= point["x_percent"] <= 100 for point in points)
    assert all(0 <= point["y_percent"] <= 100 for point in points)
    assert "not the hidden LED package" in catalog["address_map"]["meaning"]
