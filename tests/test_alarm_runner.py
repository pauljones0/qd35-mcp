from __future__ import annotations

from dataclasses import replace

import pytest

from qd35_mcp import alarm_runner
from qd35_mcp.alarms import SunriseAlarm


def alarm(**changes: object) -> SunriseAlarm:
    value = SunriseAlarm(
        alarm_id="test",
        name="Test",
        wake_time="07:00",
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
    )
    return replace(value, **changes)


class FakeService:
    def __init__(self) -> None:
        self.clock_formats: list[str] = []
        self.inputs: list[str] = []
        self.lights: list[dict[str, object]] = []
        self.volumes: list[int] = []
        self.disconnected = False

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
        "speaker_volume": None,
        "audio_started": False,
        "bluetooth_disconnected": False,
    }
    assert service.clock_formats == ["12h"]
    assert service.inputs == ["bluetooth"]
    assert service.lights[-1]["brightness"] == 80
    assert service.disconnected is True
