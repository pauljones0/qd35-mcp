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
        """Rename the speaker using 1-24 UTF-8 bytes and verify by readback."""
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
