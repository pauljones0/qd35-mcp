from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from ..service import QD35Service
from .common import call


def register_device_tools(server: MCPServer, service: QD35Service) -> None:
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
        """Check the speaker's advertised capabilities and the MCP's write gates."""
        return await call(service.capabilities)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_get_events(
        since_sequence: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=256)] = 100,
    ) -> dict[str, object]:
        """Return retained BLE notifications, including media metadata and state changes."""
        return await call(service.events, since_sequence, limit)

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
        """Restore the six DIY EQ bands to their defaults and verify the resulting gains."""
        return await call(service.reset_eq)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
    )
    async def qd35_media_control(
        action: Literal["play", "pause", "next", "previous"],
    ) -> dict[str, object]:
        """Control the active Bluetooth player. Only play and pause have state readback."""
        return await call(service.media_control, action)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
    )
    async def qd35_sync_clock(
        clock_format: Literal["preserve", "12h", "24h"] = "preserve",
    ) -> dict[str, object]:
        """Sync using the MCP's persistent timezone; firmware offers no clock readback."""
        return await call(service.sync_clock, clock_format)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_timezone(
        timezone: str,
        clock_format: Literal["preserve", "12h", "24h"] = "preserve",
        sync_now: bool = True,
    ) -> dict[str, object]:
        """Persist an IANA timezone and optionally synchronize the speaker immediately."""
        return await call(service.set_timezone, timezone, clock_format, sync_now)

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
        """Rename the speaker within its reported UTF-8 byte limit and verify by readback."""
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
