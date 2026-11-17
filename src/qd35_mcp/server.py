from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Config
from .errors import QD35Error
from .service import QD35Service
from .transport.bleak import BleakTransport
from .transport.fake import FakeTransport


def create_service(config: Config | None = None) -> QD35Service:
    config = config or Config.from_env()
    transport = (
        FakeTransport()
        if config.transport == "fake"
        else BleakTransport(adapter=config.adapter, scan_timeout=config.scan_timeout)
    )
    return QD35Service(transport, config)


def create_server(service: QD35Service | None = None) -> MCPServer:
    service = service or create_service()

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        try:
            yield {}
        finally:
            await service.disconnect()

    server = MCPServer(
        name="qd35-mcp",
        title="Edifier QD35",
        version="0.1.0",
        description="Local Bluetooth LE controls for the Edifier QD35 speaker.",
        instructions=(
            "Call qd35_get_status before changing settings. Prefer one semantic change per call, "
            "check the returned applied field, and never assume a disconnected write succeeded."
        ),
        lifespan=lifespan,
    )

    async def call(method: object, *args: object, **kwargs: object) -> dict[str, object]:
        try:
            return await method(*args, **kwargs)  # type: ignore[operator,no-any-return]
        except (QD35Error, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_discover(
        timeout_seconds: Annotated[float, Field(ge=1, le=30)] = 8,
    ) -> dict[str, object]:
        """Scan briefly for QD35 BLE control advertisements; this does not pair or connect."""
        return await call(service.discover, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_get_status() -> dict[str, object]:
        """Connect and read the speaker's current identity, audio, EQ, light, and LDAC state."""
        return await call(service.status)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_get_capabilities() -> dict[str, object]:
        """Read the device capability bytes and report the MCP's supported and excluded actions."""
        return await call(service.capabilities)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_get_events(
        since_sequence: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=256)] = 100,
    ) -> dict[str, object]:
        """Return retained BLE notifications, including media metadata and state changes."""
        return await call(service.events, since_sequence, limit)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_list_custom_lights() -> dict[str, object]:
        """List the official firmware-1.3.0 custom animations and palette keys."""
        return await call(service.custom_light_catalog)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_volume(
        level: Annotated[int, Field(ge=0, le=255)],
    ) -> dict[str, object]:
        """Set volume using the device-reported range (QD35 firmware 1.3.0 reports 0-16)."""
        return await call(service.set_volume, level)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_input(
        source: Literal["bluetooth", "aux", "usb"],
    ) -> dict[str, object]:
        """Select bluetooth, aux, or usb input and verify by readback."""
        return await call(service.set_input, source)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_eq_preset(
        preset: Literal["music", "game", "movie", "diy"],
    ) -> dict[str, object]:
        """Select music, game, movie, or diy EQ and verify by readback."""
        return await call(service.set_eq_preset, preset)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_eq_band(
        band: Annotated[int, Field(ge=0, le=5)],
        gain_db: Annotated[float, Field(ge=-3, le=3, multiple_of=0.5)],
    ) -> dict[str, object]:
        """Set one of six DIY EQ bands, preserve its frequency/Q, and activate the DIY preset."""
        return await call(service.set_eq_band, band, gain_db)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
    )
    async def qd35_reset_eq() -> dict[str, object]:
        """Reset all custom EQ bands with opcode 0x45 and verify the resulting band state."""
        return await call(service.reset_eq)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_light(
        effect: Literal[
            "ambient",
            "magic_tides",
            "shooting_star",
            "breathing",
            "tempo_emo",
            "static",
            "extended",
        ]
        | None = None,
        rgb: Annotated[str | None, Field(pattern=r"^#?[0-9A-Fa-f]{6}$")] = None,
        color_mode: Literal["light", "deep", "off", "on"] | None = None,
        brightness: Annotated[int | None, Field(ge=0, le=100)] = None,
        speed: Annotated[int | None, Field(ge=1, le=10)] = None,
        enabled: bool | None = None,
    ) -> dict[str, object]:
        """Change lighting with effect-specific RGB or named color modes."""
        return await call(service.set_light, effect, rgb, color_mode, brightness, speed, enabled)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
    )
    async def qd35_reset_light(
        effect: Literal[
            "ambient",
            "magic_tides",
            "shooting_star",
            "breathing",
            "tempo_emo",
            "static",
            "extended",
        ]
        | None = None,
    ) -> dict[str, object]:
        """Reset the selected (or named) lighting effect using the app's opcode 0x72."""
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
        """Download a hash-pinned Edifier animation, upload it, and optionally select Extended."""
        return await call(service.apply_custom_light, preset, palette_key, activate)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
    )
    async def qd35_media_control(
        action: Literal["play", "pause", "next", "previous"],
    ) -> dict[str, object]:
        """Control media and report C3 playing-state readback when it can verify the action."""
        return await call(service.media_control, action)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
    )
    async def qd35_sync_clock(
        clock_format: Literal["preserve", "12h", "24h"] = "preserve",
    ) -> dict[str, object]:
        """Set the speaker clock; firmware acknowledges success but offers no clock readback."""
        return await call(service.sync_clock, clock_format)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_ldac(
        mode: Literal["off", "48khz", "96khz"],
    ) -> dict[str, object]:
        """Set LDAC; a change restarts Bluetooth and may require re-pairing to verify."""
        return await call(service.set_ldac, mode)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_rename_device(name: str) -> dict[str, object]:
        """Rename the speaker using its D8-advertised UTF-8 byte limit and verify by readback."""
        return await call(service.rename, name)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    async def qd35_disconnect_audio() -> dict[str, object]:
        """Disconnect the Classic Bluetooth audio link. Requires QD35_ENABLE_DANGEROUS=1."""
        return await call(service.disconnect_audio)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    async def qd35_power_off() -> dict[str, object]:
        """Power the speaker off remotely. It cannot be powered on over BLE."""
        return await call(service.power_off)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    async def qd35_factory_reset() -> dict[str, object]:
        """Clear pairings and settings. Requires QD35_ENABLE_DANGEROUS=1."""
        return await call(service.factory_reset)

    return server
