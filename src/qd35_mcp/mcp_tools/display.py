from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..service import QD35Service
from .common import call


def register_display_tools(server: MCPServer, service: QD35Service) -> None:
    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_get_display_status() -> dict[str, object]:
        """Return locally retained custom-display state; patched firmware has no readback."""
        return await call(service.display_status)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_get_display_firmware_info() -> dict[str, object]:
        """Show bundled patch hashes, exact source contract, gates, and confirmation phrases."""
        return await call(service.display_firmware_info)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_display(
        power: bool | None = None,
        clock: bool | None = None,
        logo: bool | None = None,
        symbols: bool | None = None,
        brightness: Annotated[int | None, Field(ge=0, le=7)] = None,
        follow_lighting_dial: bool | None = None,
        raw_mode: bool | None = None,
    ) -> dict[str, object]:
        """Control the patched front display. Switch power off to darken both clock and logo."""
        return await call(
            service.set_display,
            power,
            clock,
            logo,
            symbols,
            brightness,
            follow_lighting_dial,
            raw_mode,
        )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            idempotentHint=True,
            destructiveHint=True,
            openWorldHint=False,
        )
    )
    async def qd35_write_display_grid_byte(
        index: Annotated[int, Field(ge=0, le=7)],
        value: Annotated[int, Field(ge=0, le=255)],
    ) -> dict[str, object]:
        """Expert control: write one raw CMS1640 byte; requires raw mode and dangerous writes."""
        return await call(service.write_display_grid_byte, index, value)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_reset_display() -> dict[str, object]:
        """Restore stock-equivalent display defaults without disconnecting Bluetooth audio."""
        return await call(service.reset_display)

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            idempotentHint=False,
            destructiveHint=True,
            openWorldHint=True,
        )
    )
    async def qd35_install_display_firmware(
        confirmation: Literal["INSTALL DISPLAY PATCH"],
    ) -> dict[str, object]:
        """Fetch the exact pinned Edifier image, apply the bundled patch, and flash this QD35."""
        if confirmation != "INSTALL DISPLAY PATCH":
            raise ToolError("confirmation must be exactly INSTALL DISPLAY PATCH")
        return await call(service.install_display_firmware_from_edifier)

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            idempotentHint=False,
            destructiveHint=True,
            openWorldHint=True,
        )
    )
    async def qd35_restore_stock_firmware(
        confirmation: Literal["RESTORE STOCK FIRMWARE"],
    ) -> dict[str, object]:
        """Fetch and flash the exact pinned official QD35 1.3.0 image, removing the patch."""
        if confirmation != "RESTORE STOCK FIRMWARE":
            raise ToolError("confirmation must be exactly RESTORE STOCK FIRMWARE")
        return await call(service.restore_stock_firmware_from_edifier)
