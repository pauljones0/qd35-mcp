import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def test_real_stdio_server_contract() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "qd35_mcp"],
        env={**os.environ, "QD35_TRANSPORT": "fake", "QD35_ENABLE_WRITES": "1"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "qd35_get_status" in names
            assert "qd35_set_light" in names
            assert "qd35_factory_reset" in names

            status = await session.call_tool("qd35_get_status")
            assert status.is_error is False
            assert status.structured_content["volume"]["maximum"] == 16

            changed = await session.call_tool("qd35_set_volume", {"level": 11})
            assert changed.is_error is False
            assert changed.structured_content["applied"] is True
            assert changed.structured_content["volume"]["current"] == 11

