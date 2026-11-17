from __future__ import annotations

from dataclasses import replace

import pytest

from qd35_mcp.alarms import (
    AlarmManager,
    AlarmStore,
    SunriseAlarm,
    SystemdUserTimers,
    alarm_start_schedule,
    bedtime_schedule,
    bedtime_systemd_calendar,
    perceptual_light_value,
    ramp_value,
    systemd_calendar,
    validate_audio_source,
)
from qd35_mcp.config import Config
from qd35_mcp.service import QD35Service
from qd35_mcp.transport.fake import FakeTransport


def fake_config(**changes: object) -> Config:
    config = Config(
        device="fake",
        adapter=None,
        transport="fake",
        writes_enabled=True,
        dangerous_writes_enabled=False,
        request_timeout=1,
        scan_timeout=1,
    )
    return replace(config, **changes)


def sample_alarm(**changes: object) -> SunriseAlarm:
    values: dict[str, object] = {
        "alarm_id": "weekday-wake",
        "name": "Weekday wake",
        "wake_time": "07:00",
        "bedtime": "22:00",
        "timezone": "America/Regina",
        "weekdays": ("mon", "tue", "wed", "thu", "fri"),
        "sunrise_minutes": 30,
        "start_brightness": 1,
        "end_brightness": 100,
        "light_rgb": "#FF6A20",
        "clock_format": "12h",
        "audio_source": None,
        "audio_fade_minutes": 10,
        "target_speaker_volume": 8,
        "ble_device": "FA:CE:D3:50:00:01",
        "classic_device": "AA:BB:CC:DD:EE:FF",
        "enabled": True,
        "created_at": "2026-11-17T00:00:00+00:00",
        "custom_display": True,
    }
    values.update(changes)
    return SunriseAlarm(**values)  # type: ignore[arg-type]


def test_schedule_starts_before_wake_in_named_timezone() -> None:
    alarm = sample_alarm()
    assert alarm_start_schedule(alarm) == (
        "06:30",
        ("mon", "tue", "wed", "thu", "fri"),
    )
    assert systemd_calendar(alarm) == ("Mon,Tue,Wed,Thu,Fri *-*-* 06:30:00 America/Regina")
    assert bedtime_schedule(alarm) == (
        "22:00",
        ("sun", "mon", "tue", "wed", "thu"),
    )
    assert bedtime_systemd_calendar(alarm) == (
        "Sun,Mon,Tue,Wed,Thu *-*-* 22:00:00 America/Regina"
    )


def test_schedule_moves_to_previous_day_across_midnight() -> None:
    alarm = sample_alarm(
        wake_time="00:15",
        sunrise_minutes=30,
        weekdays=("mon", "sun"),
    )
    assert alarm_start_schedule(alarm) == ("23:45", ("sat", "sun"))


def test_ramp_is_bounded_and_monotonic() -> None:
    assert ramp_value(1, 100, -1) == 1
    assert ramp_value(1, 100, 0.5) == 50
    assert ramp_value(1, 100, 2) == 100


def test_perceptual_light_ramp_uses_gamma_corrected_output() -> None:
    assert perceptual_light_value(0, 100, -1) == 0
    assert perceptual_light_value(0, 100, 0.25) == 5
    assert perceptual_light_value(0, 100, 0.5) == 22
    assert perceptual_light_value(0, 100, 0.75) == 53
    assert perceptual_light_value(0, 100, 2) == 100
    with pytest.raises(ValueError, match="gamma"):
        perceptual_light_value(0, 100, 0.5, gamma=0)


def test_remote_audio_is_operator_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="disabled"):
        validate_audio_source("https://radio.example/stream", fake_config())

    monkeypatch.setattr(
        "qd35_mcp.alarms.socket.getaddrinfo",
        lambda *_args: [(2, 1, 6, "", ("1.1.1.1", 443))],
    )
    config = fake_config(
        alarm_remote_audio=True,
        alarm_allowed_hosts=("radio.example",),
    )
    assert validate_audio_source("https://radio.example/stream", config) == (
        "https://radio.example/stream"
    )


class FakeTimers:
    def __init__(self) -> None:
        self.installed: list[SunriseAlarm] = []
        self.removed: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.keepalive_installed: list[str] = []
        self.keepalive_removed = 0
        self.snoozed: list[tuple[str, int]] = []

    def install(self, alarm: SunriseAlarm) -> None:
        self.installed.append(alarm)

    def remove(self, alarm_id: str) -> None:
        self.removed.append(alarm_id)

    def start_now(self, alarm_id: str) -> None:
        self.started.append(alarm_id)

    def stop(self, alarm_id: str) -> None:
        self.stopped.append(alarm_id)

    def snooze(self, alarm_id: str, minutes: int) -> None:
        self.stopped.append(alarm_id)
        self.snoozed.append((alarm_id, minutes))

    def state(self, _alarm_id: str) -> dict[str, object]:
        return {
            "timer_enabled": True,
            "service_active": False,
            "preparation_timer_enabled": True,
        }

    def keepalive_state(self) -> dict[str, object]:
        return {
            "enabled": bool(self.keepalive_installed) and self.keepalive_removed == 0,
            "active": bool(self.keepalive_installed) and self.keepalive_removed == 0,
        }

    def install_keepalive(self, classic_device: str) -> None:
        self.keepalive_installed.append(classic_device)

    def remove_keepalive(self) -> None:
        self.keepalive_removed += 1


async def test_manager_persists_and_controls_alarm(tmp_path) -> None:
    config = fake_config(alarm_state_path=str(tmp_path / "alarms.json"))
    store = AlarmStore(tmp_path / "alarms.json")
    timers = FakeTimers()
    manager = AlarmManager(
        QD35Service(FakeTransport(), config),
        config,
        store=store,
        timers=timers,  # type: ignore[arg-type]
    )
    created = await manager.create(
        "Weekday Wake",
        "07:00",
        "22:00",
        "America/Regina",
        ["mon", "tue", "wed", "thu", "fri"],
        30,
        1,
        100,
        "#FF6A20",
        "12h",
        None,
        10,
        8,
    )
    assert created["alarm_id"] == "weekday-wake"
    assert created["installed"] is True
    assert created["bedtime_schedule"].startswith("Sun,Mon,Tue,Wed,Thu")
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert timers.installed[0].clock_format == "12h"

    listed = await manager.list()
    assert listed["count"] == 1
    assert listed["alarms"][0]["timer_enabled"] is True
    keepalive = await manager.set_audio_keepalive(True)
    assert keepalive["active"] is True
    assert keepalive["speaker_disconnect_command_sent"] is False
    assert (await manager.start_now("weekday-wake"))["started"] is True
    assert (await manager.stop("weekday-wake"))["bluetooth_disconnected"] is False
    snoozed = await manager.snooze("weekday-wake", 10)
    assert snoozed["snoozed"] is True
    assert snoozed["night_light_applied"] is True
    assert (await manager.delete("weekday-wake"))["deleted"] is True
    assert timers.started == ["weekday-wake"]
    assert timers.stopped == ["weekday-wake", "weekday-wake"]
    assert timers.snoozed == [("weekday-wake", 10)]
    assert timers.removed == ["weekday-wake"]
    assert timers.keepalive_removed == 1


def test_systemd_units_include_separate_bedtime_preparation(tmp_path, monkeypatch) -> None:
    timers = SystemdUserTimers(tmp_path / "units", tmp_path / "alarms.json")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(timers, "_run", lambda *args, **_kwargs: calls.append(args))
    timers.install(sample_alarm())
    prepare_service = tmp_path / "units/qd35-alarm-weekday-wake-prepare.service"
    prepare_timer = tmp_path / "units/qd35-alarm-weekday-wake-prepare.timer"
    assert "--prepare" in prepare_service.read_text(encoding="utf-8")
    assert "OnCalendar=Sun,Mon,Tue,Wed,Thu *-*-* 22:00:00 America/Regina" in (
        prepare_timer.read_text(encoding="utf-8")
    )
    assert ("enable", "--now", "qd35-alarm-weekday-wake-prepare.timer") in calls
    snooze_service = tmp_path / "units/qd35-alarm-weekday-wake-snooze.service"
    assert "--snoozed" in snooze_service.read_text(encoding="utf-8")


def test_systemd_snooze_timer_targets_short_fade_service(tmp_path, monkeypatch) -> None:
    timers = SystemdUserTimers(tmp_path / "units", tmp_path / "alarms.json")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(timers, "_run", lambda *args, **_kwargs: calls.append(args))
    timers.install(sample_alarm())
    timers.snooze("weekday-wake", 10)
    timer = tmp_path / "units/qd35-alarm-weekday-wake-snooze.timer"
    text = timer.read_text(encoding="utf-8")
    assert "OnActiveSec=10min" in text
    assert "Unit=qd35-alarm-weekday-wake-snooze.service" in text
    assert ("start", "qd35-alarm-weekday-wake-snooze.timer") in calls
