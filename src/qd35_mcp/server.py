"""Compose and serve the public MCP surface."""

from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from . import __version__
from .alarms import AlarmManager
from .factory import create_service as create_service
from .mcp_tools.alarms import register_alarm_tools
from .mcp_tools.device import register_device_tools
from .mcp_tools.display import register_display_tools
from .mcp_tools.guide import SERVER_INSTRUCTIONS, register_guide
from .mcp_tools.lighting import register_lighting_tools
from .service import QD35Service


def create_server(
    service: QD35Service | None = None,
    alarm_manager: AlarmManager | None = None,
) -> MCPServer:
    service = service or create_service()
    alarm_manager = alarm_manager or AlarmManager(service, service.config)

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        try:
            yield {}
        finally:
            await service.disconnect()

    server = MCPServer(
        name="qd35-mcp",
        title="Edifier QD35",
        version=__version__,
        description="Local controls and sunrise alarms for the Edifier QD35 speaker.",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
    )
    register_device_tools(server, service)
    register_lighting_tools(server, service)
    register_alarm_tools(server, alarm_manager)
    register_display_tools(server, service)
    register_guide(server)
    return server
