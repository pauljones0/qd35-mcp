from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..service import QD35Service
from .common import call

RGBColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
LightingEffect = Literal[
    "ambient", "magic_tides", "shooting_star", "breathing", "tempo_emo", "static", "extended"
]


class AnimationFrameInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_ms: Annotated[int, Field(ge=5, le=60_000)]
    colors: Annotated[list[RGBColor], Field(min_length=37, max_length=37)]


def register_lighting_tools(server: MCPServer, service: QD35Service) -> None:
    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_list_custom_lights() -> dict[str, object]:
        """List official downloads, creative presets, diagnostic patterns, and palette keys."""
        return await call(service.custom_light_catalog)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_light(
        effect: LightingEffect | None = None,
        rgb: Annotated[str | None, Field(pattern=r"^#?[0-9A-Fa-f]{6}$")] = None,
        color_mode: Literal["light", "deep", "off", "on"] | None = None,
        brightness: Annotated[int | None, Field(ge=0, le=100)] = None,
        speed: Annotated[int | None, Field(ge=1, le=10)] = None,
        enabled: bool | None = None,
    ) -> dict[str, object]:
        """Change the selected effect, RGB or named color mode, and brightness (0-100)."""
        return await call(service.set_light, effect, rgb, color_mode, brightness, speed, enabled)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
    )
    async def qd35_reset_light(
        effect: LightingEffect | None = None,
    ) -> dict[str, object]:
        """Restore default parameters for the selected or named lighting effect."""
        return await call(service.reset_light, effect)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_light_timer(
        index: Annotated[int, Field(ge=0, le=255)],
        value: Annotated[int, Field(ge=0, le=255)],
    ) -> dict[str, object]:
        """Set the app-defined light timer only when the speaker advertises that capability."""
        return await call(service.set_light_timer, index, value)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=True)
    )
    async def qd35_apply_custom_light(
        preset: Literal["gradient", "water_drop", "spiral", "multicolored"],
        palette_key: Annotated[int | None, Field(ge=1, le=11)] = None,
        activate: bool = True,
    ) -> dict[str, object]:
        """Replace the Extended asset with an official animation and optionally select it."""
        return await call(service.apply_custom_light, preset, palette_key, activate)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_apply_diagnostic_light(
        pattern: Literal[
            "pixel_chase",
            "address_mapper",
            "rgb_bars",
            "timing_sweep",
            "fourth_byte_sweep",
            "spiral_color_probe",
            "spiral_index_probe",
            "spiral_fourth_probe",
            "spiral_fast_probe",
            "spiral_slow_probe",
        ],
        brightness: Annotated[int, Field(ge=1, le=25)] = 10,
        activate: bool = True,
    ) -> dict[str, object]:
        """Upload a bounded diagnostic animation for video-assisted protocol calibration."""
        return await call(service.apply_custom_light, pattern, None, activate, brightness)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_apply_authored_light(
        name: Annotated[str, Field(min_length=1, max_length=48)],
        frames: Annotated[list[AnimationFrameInput], Field(min_length=1, max_length=102)],
        brightness: Annotated[int, Field(ge=1, le=100)] = 10,
        activate: bool = True,
    ) -> dict[str, object]:
        """Compile and upload bounded local RGB frames; no file paths or URLs are accepted."""
        compiled = [(frame.duration_ms, frame.colors) for frame in frames]
        return await call(service.apply_authored_light, name, compiled, activate, brightness)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_apply_animation_preset(
        preset: Literal["pac_man", "comet", "rainbow_wave", "fireflies", "embers"],
        brightness: Annotated[int, Field(ge=1, le=100)] = 15,
        activate: bool = True,
    ) -> dict[str, object]:
        """Generate and upload a built-in creative animation with no network or file input."""
        return await call(service.apply_animation_preset, preset, activate, brightness)
