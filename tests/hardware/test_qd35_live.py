"""Opt-in tests for a physical QD35.

Run read checks with QD35_HARDWARE=1 and set QD35_DEVICE when needed.
Add QD35_HARDWARE_WRITES=1 and QD35_ENABLE_WRITES=1 for reversible mutations.
"""

from __future__ import annotations

import os

import pytest

from qd35_mcp import constants as c
from qd35_mcp.server import create_service


def enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


pytestmark = pytest.mark.skipif(
    not enabled("QD35_HARDWARE"),
    reason="set QD35_HARDWARE=1 to run physical-speaker tests",
)


async def test_live_identity_and_state_shape() -> None:
    service = create_service()
    try:
        status = await service.status()
        assert status["name"]
        assert status["firmware"]
        assert status["device"]["protocol_version"] == 2
        assert status["device"]["encryption_selector"] in (0, 0x10)
        assert status["volume"]["current"] <= status["volume"]["maximum"]
        assert status["input"]["group"] == 2
        assert status["custom_eq"]["band_count"] == 6
        assert [band["index"] for band in status["custom_eq"]["bands"]] == list(range(6))
        assert status["lighting"]["group"] == 1
        assert {effect["id"] for effect in status["lighting"]["effects"]} == set(range(1, 8))
        assert isinstance(status["playback"]["playing"], bool)
    finally:
        await service.disconnect()


async def test_live_reversible_control_matrix() -> None:
    if not enabled("QD35_HARDWARE_WRITES") or not enabled("QD35_ENABLE_WRITES"):
        pytest.skip("set QD35_HARDWARE_WRITES=1 and QD35_ENABLE_WRITES=1")

    service = create_service()
    original = await service.status()
    original_lights = {item["id"]: item for item in original["lighting"]["effects"]}

    async def restore() -> None:
        await service.set_volume(original["volume"]["current"])
        await service.set_input(original["input"]["name"])
        for band in original["custom_eq"]["bands"]:
            await service.set_eq_band(band["index"], band["gain_db"])
        await service.set_eq_preset(original["eq_preset"]["name"])
        for effect, effect_id in c.LIGHT_EFFECTS.items():
            state = original_lights[effect_id]
            kwargs: dict[str, object] = {
                "effect": effect,
                "brightness": state["brightness"],
            }
            if state["color_control"] == "rgb":
                kwargs["rgb"] = state["rgb"]
            else:
                kwargs["color_mode"] = state["color_mode"]
            if state["speed"] is not None:
                kwargs["speed"] = state["speed"]
            await service.set_light(**kwargs)
        await service.set_light(effect=original["lighting"]["selected_effect"])
        await service.rename(original["name"])

    try:
        old_volume = original["volume"]["current"]
        target_volume = old_volume - 1 if old_volume else 1
        assert (await service.set_volume(target_volume))["applied"] is True
        assert (await service.set_volume(old_volume))["applied"] is True

        for source in c.INPUTS:
            assert (await service.set_input(source))["applied"] is True
        assert (await service.set_input(original["input"]["name"]))["applied"] is True

        for preset in c.EQ_PRESETS:
            assert (await service.set_eq_preset(preset))["applied"] is True
        for band in original["custom_eq"]["bands"]:
            target = -0.5 if band["gain_db"] >= 0 else 0.5
            assert (await service.set_eq_band(band["index"], target))["applied"] is True
            assert (await service.set_eq_band(band["index"], band["gain_db"]))["applied"] is True

        for effect, effect_id in c.LIGHT_EFFECTS.items():
            kwargs: dict[str, object] = {
                "effect": effect,
                "brightness": 90,
            }
            if effect_id in c.LIGHT_RGB_EFFECT_IDS:
                kwargs["rgb"] = "#123456"
            else:
                kwargs["color_mode"] = "deep" if effect_id in (1, 2) else "on"
            if original_lights[effect_id]["speed"] is not None:
                kwargs["speed"] = 1
            assert (await service.set_light(**kwargs))["applied"] is True

        assert (await service.rename("QD35 MCP TEST"))["applied"] is True
        assert (await service.rename(original["name"]))["applied"] is True
        clock = await service.sync_clock("preserve")
        assert clock["acknowledged"] is True
        assert clock["applied"] is None
    finally:
        try:
            await restore()
        finally:
            await service.disconnect()
