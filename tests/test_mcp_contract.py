import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from qd35_mcp.animations import DIAGNOSTIC_ANIMATION_NAMES
from qd35_mcp.config import Config
from qd35_mcp.server import create_server
from qd35_mcp.service import QD35Service
from qd35_mcp.transport.fake import FakeTransport


async def test_server_lifespan_releases_bluetooth_session() -> None:
    transport = FakeTransport()
    service = QD35Service(
        transport,
        Config(
            device=None,
            adapter=None,
            transport="fake",
            writes_enabled=True,
            dangerous_writes_enabled=True,
            request_timeout=1,
            scan_timeout=1,
        ),
    )
    await service.status()
    assert transport.connected is True
    server = create_server(service)

    async with server._lowlevel_server.lifespan(server._lowlevel_server):
        assert transport.connected is True

    assert transport.connected is False


async def test_real_stdio_server_contract(tmp_path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "qd35_mcp"],
        env={
            **os.environ,
            "QD35_TRANSPORT": "fake",
            "QD35_ENABLE_WRITES": "1",
            "QD35_ENABLE_DANGEROUS": "1",
            "QD35_TIMEZONE_STATE_PATH": str(tmp_path / "timezone.json"),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resources = await session.list_resources()
            assert [str(resource.uri) for resource in resources.resources] == ["qd35://guide"]
            guide = await session.read_resource("qd35://guide")
            assert "qd35_get_status" in guide.contents[0].text
            assert "qd35_list_alarms" in guide.contents[0].text
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "qd35_get_status" in names
            assert "qd35_set_light" in names
            assert "qd35_apply_custom_light" in names
            assert "qd35_apply_diagnostic_light" in names
            assert "qd35_apply_authored_light" in names
            assert "qd35_apply_reactive_light" in names
            assert "qd35_apply_animation_preset" in names
            assert "qd35_create_sunrise_alarm" in names
            assert "qd35_list_alarms" in names
            assert "qd35_set_alarm_audio_keepalive" in names
            assert "qd35_set_timezone" in names
            assert "qd35_get_events" in names
            assert "qd35_factory_reset" in names
            assert "qd35_get_display_status" in names
            assert "qd35_get_display_firmware_info" in names
            assert "qd35_set_display" in names
            assert "qd35_write_display_grid_byte" in names
            assert "qd35_reset_display" in names
            assert "qd35_install_display_firmware" in names
            assert "qd35_restore_stock_firmware" in names

            by_name = {tool.name: tool for tool in tools.tools}
            assert by_name["qd35_set_input"].input_schema["properties"]["source"]["enum"] == [
                "bluetooth",
                "aux",
                "usb",
            ]
            assert by_name["qd35_set_eq_preset"].input_schema["properties"]["preset"]["enum"] == [
                "music",
                "game",
                "movie",
                "diy",
            ]
            assert by_name["qd35_media_control"].input_schema["properties"]["action"]["enum"] == [
                "play",
                "pause",
                "next",
                "previous",
            ]
            assert by_name["qd35_set_light"].input_schema["properties"]["color_mode"]["anyOf"][0][
                "enum"
            ] == ["light", "deep", "off", "on"]
            assert by_name["qd35_apply_diagnostic_light"].input_schema["properties"]["pattern"][
                "enum"
            ] == list(DIAGNOSTIC_ANIMATION_NAMES)
            assert by_name["qd35_sync_clock"].annotations.idempotent_hint is False
            alarm_properties = by_name["qd35_create_sunrise_alarm"].input_schema[
                "properties"
            ]
            assert "bedtime" in alarm_properties
            assert alarm_properties["physical_snooze_minutes"]["default"] == 10
            assert alarm_properties["post_wake_minutes"]["default"] == 30
            assert alarm_properties["fade_out_minutes"]["default"] == 2
            assert by_name["qd35_install_display_firmware"].input_schema["properties"][
                "confirmation"
            ]["const"] == "INSTALL DISPLAY PATCH"
            display_properties = by_name["qd35_set_display"].input_schema["properties"]
            assert "line" in display_properties
            assert "Legacy alias" in display_properties["logo"]["description"]

            status = await session.call_tool("qd35_get_status")
            assert status.is_error is False
            assert status.structured_content["volume"]["maximum"] == 16

            changed = await session.call_tool("qd35_set_volume", {"level": 11})
            assert changed.is_error is False
            assert changed.structured_content["applied"] is True
            assert changed.structured_content["volume"]["current"] == 11

            invalid = await session.call_tool("qd35_set_input", {"source": "optical"})
            assert invalid.is_error is True

            line = await session.call_tool("qd35_set_display", {"line": False})
            assert line.is_error is False
            assert line.structured_content["state"]["line"] is False
            conflicting = await session.call_tool("qd35_set_display", {"line": True, "logo": False})
            assert conflicting.is_error is True

            successful_calls = [
                ("qd35_discover", {}),
                ("qd35_get_capabilities", {}),
                ("qd35_get_events", {}),
                ("qd35_list_custom_lights", {}),
                ("qd35_set_input", {"source": "usb"}),
                ("qd35_set_eq_preset", {"preset": "movie"}),
                ("qd35_set_eq_band", {"band": 0, "gain_db": 0.5}),
                ("qd35_reset_eq", {}),
                (
                    "qd35_set_light",
                    {"effect": "static", "rgb": "#123456", "brightness": 40},
                ),
                (
                    "qd35_set_display",
                    {"clock": False, "logo": True, "brightness": 3},
                ),
                ("qd35_write_display_grid_byte", {"index": 0, "value": 165}),
                ("qd35_reset_display", {}),
                (
                    "qd35_apply_diagnostic_light",
                    {"pattern": "rgb_bars", "brightness": 10},
                ),
                (
                    "qd35_apply_authored_light",
                    {
                        "name": "Contract loop",
                        "frames": [
                            {"duration_ms": 250, "colors": ["#123456"] * 37}
                        ],
                        "brightness": 10,
                    },
                ),
                (
                    "qd35_apply_animation_preset",
                    {"preset": "pac_man", "brightness": 8},
                ),
                (
                    "qd35_apply_reactive_light",
                    {"name": "Reactive test", "bands": [[["#123456"] * 37] * 10] * 10},
                ),
                ("qd35_media_control", {"action": "pause"}),
                ("qd35_sync_clock", {"clock_format": "24h"}),
                ("qd35_set_ldac", {"mode": "48khz"}),
                ("qd35_rename_device", {"name": "QD35 Test"}),
                ("qd35_disconnect_audio", {}),
                ("qd35_factory_reset", {}),
                ("qd35_power_off", {}),
            ]
            for tool_name, arguments in successful_calls:
                result = await session.call_tool(tool_name, arguments)
                assert result.is_error is False, tool_name
