from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from qd35_mcp import alarm_runner
from qd35_mcp.alarms import SunriseAlarm


def alarm(**changes: object) -> SunriseAlarm:
    value = SunriseAlarm(
        alarm_id="test",
        name="Test",
        wake_time="07:00",
        bedtime="22:00",
        timezone="America/Regina",
        weekdays=("mon",),
        sunrise_minutes=0,
        start_brightness=1,
        end_brightness=80,
        light_rgb="#FF6A20",
        clock_format="12h",
        audio_source=None,
        audio_fade_minutes=0,
        target_speaker_volume=8,
        ble_device="FA:CE:D3:50:00:01",
        classic_device="AA:BB:CC:DD:EE:FF",
        enabled=True,
        created_at="2026-11-17T00:00:00+00:00",
        custom_display=True,
    )
    return replace(value, **changes)


class FakeService:
    def __init__(self) -> None:
        self.clock_formats: list[str] = []
        self.inputs: list[str] = []
        self.lights: list[dict[str, object]] = []
        self.volumes: list[int] = []
        self.displays: list[dict[str, object]] = []
        self.disconnected = False
        self.event_calls = 0
        self.emit_manual_control = False

    async def sync_clock(self, value: str, timezone: str) -> dict[str, object]:
        self.clock_formats.append(value)
        assert timezone == "America/Regina"
        return {"acknowledged": True}

    async def set_timezone(self, value: str, sync_now: bool) -> dict[str, object]:
        assert sync_now is False
        return {"timezone": value, "persisted": True}

    async def set_input(self, value: str) -> dict[str, object]:
        self.inputs.append(value)
        return {"applied": True}

    async def set_light(self, **values: object) -> dict[str, object]:
        self.lights.append(values)
        return {"applied": True}

    async def set_volume(self, value: int) -> dict[str, object]:
        self.volumes.append(value)
        return {"applied": True}

    async def set_display(self, **values: object) -> dict[str, object]:
        self.displays.append(values)
        return {"acknowledged": True}

    async def events(
        self, since_sequence: int = 0, limit: int = 100
    ) -> dict[str, object]:
        self.event_calls += 1
        events: list[dict[str, object]] = []
        latest = 0
        if self.emit_manual_control and self.event_calls >= 2:
            latest = 1
            if since_sequence < 1:
                events.append(
                    {
                        "sequence": 1,
                        "session": 1,
                        "opcode": alarm_runner.c.QUERY_VOLUME,
                        "payload_hex": "1004",
                    }
                )
        return {
            "events": events[:limit],
            "latest_sequence": latest,
            "count": len(events[:limit]),
        }

    async def disconnect(self) -> None:
        self.disconnected = True


async def test_runner_completes_lighting_alarm_without_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService()
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    result = await alarm_runner.run_alarm(alarm())
    assert result == {
        "alarm_id": "test",
        "completed": True,
        "brightness": 80,
        "display_brightness": 7,
        "speaker_volume": None,
        "audio_started": False,
        "bluetooth_disconnected": False,
    }
    assert service.clock_formats == ["12h"]
    assert service.inputs == []
    assert service.lights[-1]["brightness"] == 80
    assert service.displays[-1]["brightness"] == 7
    assert service.disconnected is True


async def test_bedtime_preparation_sets_low_light_without_classic_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService()
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    result = await alarm_runner.prepare_overnight(alarm(start_brightness=3))
    assert result["prepared"] is True
    assert result["brightness"] == 0
    assert result["display_brightness"] == 0
    assert result["clock_visible"] is False
    assert result["logo_visible"] is False
    assert result["classic_audio_touched"] is False
    assert result["bluetooth_disconnect_command_sent"] is False
    assert service.inputs == []
    assert service.volumes == []
    assert service.lights == [
        {"effect": "static", "rgb": "#FF6A20", "brightness": 0}
    ]
    assert service.displays == [
        {
            "power": False,
            "clock": False,
            "logo": False,
            "symbols": False,
            "brightness": 0,
            "follow_lighting_dial": False,
            "raw_mode": False,
        }
    ]


async def test_side_control_event_snoozes_and_restores_dark_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService()
    service.emit_manual_control = True
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)

    result = await alarm_runner.run_alarm(
        alarm(sunrise_minutes=1, physical_snooze_minutes=10), tick_seconds=0.01
    )

    assert result["snoozed_by_physical_control"] is True
    assert result["snooze_minutes"] == 10
    assert result["control_event"]["opcode"] == alarm_runner.c.QUERY_VOLUME  # type: ignore[index]
    assert service.lights[-1]["brightness"] == 0
    assert service.displays[-1]["power"] is False
    assert result["bluetooth_disconnected"] is False


class FakePlayer:
    pid = 4242

    def __init__(self) -> None:
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


async def test_audio_alarm_releases_classic_after_post_wake_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService()
    player = FakePlayer()
    release = {"disconnected": True, "keepalive": {"applied": True}}
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _alarm: _async_value(player))
    monkeypatch.setattr(
        alarm_runner,
        "_release_classic_audio",
        lambda _address: _async_value(release),
    )

    result = await alarm_runner.run_alarm(
        alarm(audio_source="https://radio.example/stream", post_wake_minutes=0)
    )

    assert result["audio_started"] is True
    assert result["bluetooth_disconnected"] is True
    assert result["classic_release"] == release
    assert service.inputs == ["bluetooth"]


async def test_bedtime_reenables_keepalive_for_audio_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService()
    requests: list[bool] = []
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)

    async def keepalive(enabled: bool) -> dict[str, object]:
        requests.append(enabled)
        return {"requested": enabled, "applied": True}

    async def connected(_address: str) -> bool:
        return True

    monkeypatch.setattr(alarm_runner, "_set_keepalive_enabled", keepalive)
    monkeypatch.setattr(alarm_runner, "_wait_for_classic_connection", connected)
    result = await alarm_runner.prepare_overnight(
        alarm(audio_source="https://radio.example/stream")
    )

    assert requests == [True]
    assert result["classic_audio_touched"] is True
    assert result["classic_keepalive"] == {"requested": True, "applied": True}


async def test_start_audio_selects_exact_native_pipewire_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = FakePlayer()
    command: list[str] = []

    async def connected(_address: str) -> None:
        return None

    async def sink(_address: str) -> str:
        return "bluez_output.AA_BB_CC_DD_EE_FF.1"

    async def launch(*arguments: str, **_kwargs: object) -> FakePlayer:
        command.extend(arguments)
        return player

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(alarm_runner, "_connect_classic", connected)
    monkeypatch.setattr(alarm_runner, "_find_audio_sink", sink)
    monkeypatch.setattr(alarm_runner.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(alarm_runner.asyncio, "sleep", no_sleep)

    started = await alarm_runner._start_audio(
        alarm(audio_source="https://radio.example/stream")
    )

    assert started is player
    assert "--no-config" in command
    assert "--msg-level=all=warn" in command
    assert "--audio-device=pipewire/bluez_output.AA_BB_CC_DD_EE_FF.1" in command
    assert command[-2:] == ["--", "https://radio.example/stream"]


async def test_connect_classic_skips_redundant_connect_when_already_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    async def command(*arguments: str, **_kwargs: object) -> tuple[int, str]:
        commands.append(arguments)
        return 0, "Connected: yes"

    monkeypatch.setattr(alarm_runner, "_run_command", command)

    await alarm_runner._connect_classic("AA:BB:CC:DD:EE:FF")

    assert commands == [("bluetoothctl", "info", "AA:BB:CC:DD:EE:FF")]


async def test_release_does_not_disconnect_when_keepalive_cannot_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    async def keepalive(_enabled: bool) -> dict[str, object]:
        return {"applied": False, "detail": "failed"}

    async def command(*arguments: str, **_kwargs: object) -> tuple[int, str]:
        commands.append(arguments)
        return 0, "Disconnection successful"

    monkeypatch.setattr(alarm_runner, "_set_keepalive_enabled", keepalive)
    monkeypatch.setattr(alarm_runner, "_run_command", command)

    result = await alarm_runner._release_classic_audio("AA:BB:CC:DD:EE:FF")

    assert result["disconnected"] is False
    assert result["disconnect_exit"] is None
    assert commands == []


async def _async_value(value: Any) -> Any:
    return value
