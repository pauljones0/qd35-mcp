"""Usage guidance available to MCP clients without connecting to the speaker."""

from mcp.server.mcpserver import MCPServer

SERVER_INSTRUCTIONS = (
    "Use qd35_get_status for current speaker state and qd35_list_alarms for saved alarms. "
    "For each change, inspect the returned readback or outcome. An acknowledgement alone "
    "does not verify a physical change. Read qd35://guide for workflows and tool groups."
)

GUIDE = """# Edifier QD35 tool guide

- **Start here:** `qd35_get_status` reads device state. Use `qd35_discover` if the device cannot
  be found and `qd35_get_capabilities` to check device support and write gates.
- **Audio and time:** use `qd35_set_volume`, `qd35_set_input`, and the EQ tools for settings.
  `qd35_media_control` operates the active Bluetooth player. `qd35_sync_clock` uses the current
  IANA timezone. Use `qd35_set_timezone` only when changing the saved zone.
- **Lighting:** use `qd35_set_light` for ordinary effects. For downloadable or generated
  animations, start with `qd35_list_custom_lights`. An upload replaces the single Extended asset.
  Use diagnostic patterns for calibration and creative presets for everyday use.
- **Alarms:** start with `qd35_list_alarms` to find the alarm ID and schedule. Check the
  timezone, wake time, audio source, and volume before creating or replacing an alarm.
- **Front display:** display controls require the optional patch. The lit clock can keep the
  wordmark visible even with its grid masked. For a fully dark front, turn display power off.
- **Maintenance:** factory reset clears pairings and settings. Firmware install and restore
  require the exact supported image and separate operator gates. Ask before disruptive actions.

`applied: true` means the reported verification passed. An unknown outcome after a write calls
for a separate read or user observation before repeating the action.
"""


def register_guide(server: MCPServer) -> None:
    @server.resource(
        "qd35://guide",
        title="QD35 tool guide",
        description="Everyday controls, alarm workflows, and advanced operation limits.",
        mime_type="text/markdown",
    )
    def guide() -> str:
        return GUIDE
