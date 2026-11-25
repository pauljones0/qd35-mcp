from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from ..alarms import AlarmManager
from .common import call


def register_alarm_tools(server: MCPServer, alarm_manager: AlarmManager) -> None:
    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def qd35_list_alarms() -> dict[str, object]:
        """List persistent sunrise alarms and their systemd timer/runtime state."""
        return await call(alarm_manager.list)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_set_alarm_audio_keepalive(enabled: bool) -> dict[str, object]:
        """Keep the paired Classic audio endpoint connected/reconnected overnight."""
        return await call(alarm_manager.set_audio_keepalive, enabled)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=True)
    )
    async def qd35_create_sunrise_alarm(
        name: Annotated[str, Field(min_length=1, max_length=80)],
        wake_time: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")],
        weekdays: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]],
        bedtime: Annotated[
            str | None, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
        ] = None,
        timezone: str = "America/Regina",
        sunrise_minutes: Annotated[int, Field(ge=1, le=180)] = 30,
        start_brightness: Annotated[int, Field(ge=0, le=99)] = 0,
        end_brightness: Annotated[int, Field(ge=1, le=100)] = 100,
        light_rgb: Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")] = "#FF6A20",
        clock_format: Literal["preserve", "12h", "24h"] = "12h",
        audio_source: str | None = None,
        audio_fade_minutes: Annotated[int, Field(ge=0, le=180)] = 10,
        target_speaker_volume: Annotated[int, Field(ge=0, le=8)] = 4,
        physical_snooze_minutes: Annotated[int, Field(ge=1, le=60)] = 10,
        post_wake_minutes: Annotated[int, Field(ge=0, le=180)] = 30,
        fade_out_minutes: Annotated[int, Field(ge=0, le=60)] = 2,
    ) -> dict[str, object]:
        """Create or replace a sunrise alarm by name. Wake time is the end of the light ramp."""
        return await call(
            alarm_manager.create,
            name,
            wake_time,
            bedtime,
            timezone,
            weekdays,
            sunrise_minutes,
            start_brightness,
            end_brightness,
            light_rgb,
            clock_format,
            audio_source,
            audio_fade_minutes,
            target_speaker_volume,
            physical_snooze_minutes,
            post_wake_minutes,
            fade_out_minutes,
        )

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
    )
    async def qd35_run_alarm_now(alarm_id: str) -> dict[str, object]:
        """Start one installed alarm immediately through its systemd user service."""
        return await call(alarm_manager.start_now, alarm_id)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def qd35_stop_alarm(alarm_id: str) -> dict[str, object]:
        """Stop an active alarm without issuing a Bluetooth disconnect command."""
        return await call(alarm_manager.stop, alarm_id)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
    )
    async def qd35_snooze_alarm(
        alarm_id: str,
        minutes: Annotated[int, Field(ge=1, le=60)] = 10,
    ) -> dict[str, object]:
        """Stop audio and darken the speaker, then resume before the original alarm cutoff."""
        return await call(alarm_manager.snooze, alarm_id, minutes)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    async def qd35_delete_alarm(alarm_id: str) -> dict[str, object]:
        """Delete one alarm and remove its systemd user timer/service files."""
        return await call(alarm_manager.delete, alarm_id)
