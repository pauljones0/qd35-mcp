from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from qd35_mcp import alarms as alarm_module
from qd35_mcp.alarms import (
    AlarmManager,
    AlarmStore,
    SunriseAlarm,
    SystemdUserTimers,
    active_wake_window,
    alarm_start_schedule,
    bedtime_schedule,
    bedtime_systemd_calendar,
    in_bedtime_window,
    perceptual_light_value,
    ramp_value,
    systemd_calendar,
    validate_audio_source,
)
from qd35_mcp.config import Config
from qd35_mcp.errors import WritesDisabled
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


def test_bedtime_uses_last_occurrence_before_ramp() -> None:
    alarm = sample_alarm(bedtime="06:45", weekdays=("mon",))
    assert bedtime_schedule(alarm) == ("06:45", ("sun",))
    assert in_bedtime_window(alarm, datetime(2026, 11, 15, 13, tzinfo=UTC))
    assert not in_bedtime_window(alarm, datetime(2026, 11, 16, 12, 30, tzinfo=UTC))


def test_wake_window_handles_midnight_and_expires_after_playback() -> None:
    alarm = sample_alarm(wake_time="00:15", weekdays=("mon",), audio_source="/radio.mp3")
    start = datetime(2026, 11, 16, 5, 45, tzinfo=UTC)
    wake = datetime(2026, 11, 16, 6, 15, tzinfo=UTC)
    assert active_wake_window(alarm, start) == (start, wake)
    assert active_wake_window(alarm, datetime(2026, 11, 16, 6, 44, tzinfo=UTC)) == (start, wake)
    assert active_wake_window(alarm, datetime(2026, 11, 16, 6, 45, tzinfo=UTC)) is None
    assert active_wake_window(replace(alarm, audio_source=None), wake) is None


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
        self.cutoff: datetime | None = None
        self.active = False
        self.snooze_pending = False
        self.snooze_running = False

    def install(self, alarm: SunriseAlarm) -> None:
        self.installed.append(alarm)

    def remove(self, alarm_id: str) -> None:
        self.removed.append(alarm_id)

    def start_now(self, alarm_id: str) -> None:
        self.started.append(alarm_id)
        self.active = True
        self.snooze_pending = False

    def stop(self, alarm_id: str) -> None:
        self.stopped.append(alarm_id)
        self.active = self.snooze_running = self.snooze_pending = False

    def snooze(self, alarm_id: str, minutes: int, *, cutoff: datetime | None = None) -> None:
        self.stop(alarm_id)
        self.snoozed.append((alarm_id, minutes))
        self.cutoff = cutoff
        self.snooze_pending = True

    def snooze_cutoff(self, _alarm_id: str) -> datetime | None:
        return self.cutoff

    def state(self, _alarm_id: str) -> dict[str, object]:
        return {
            "timer_enabled": True,
            "service_active": self.active or self.snooze_running,
            "preparation_timer_enabled": True,
            "snooze_active": self.snooze_pending,
            "snooze_service_active": self.snooze_running,
        }

    def keepalive_state(self) -> dict[str, object]:
        return {
            "enabled": bool(self.keepalive_installed) and self.keepalive_removed == 0,
            "active": bool(self.keepalive_installed) and self.keepalive_removed == 0,
        }

    def install_keepalive(self, classic_device: str, *, start: bool = True) -> None:
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
    assert timers.installed[0].physical_snooze_minutes == 10
    assert timers.installed[0].post_wake_minutes == 30
    assert timers.installed[0].fade_out_minutes == 2

    listed = await manager.list()
    assert listed["count"] == 1
    assert listed["alarms"][0]["timer_enabled"] is True
    keepalive = await manager.set_audio_keepalive(True)
    assert keepalive["active"] is True
    assert keepalive["speaker_disconnect_command_sent"] is False
    assert (await manager.start_now("weekday-wake"))["started"] is True
    snoozed = await manager.snooze("weekday-wake", 10)
    assert snoozed["snoozed"] is True
    assert snoozed["night_light_applied"] is True
    assert (await manager.stop("weekday-wake"))["bluetooth_disconnected"] is False
    assert (await manager.delete("weekday-wake"))["deleted"] is True
    assert timers.started == ["weekday-wake"]
    assert timers.stopped == ["weekday-wake", "weekday-wake"]
    assert timers.snoozed == [("weekday-wake", 10)]
    assert timers.removed == ["weekday-wake"]
    assert timers.keepalive_removed == 0


def test_systemd_units_include_separate_bedtime_preparation(tmp_path, monkeypatch) -> None:
    timers = SystemdUserTimers(tmp_path / "units", tmp_path / "alarms.json")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(timers, "_run", lambda *args, **_kwargs: calls.append(args))
    timers.install(sample_alarm())
    prepare_service = tmp_path / "units/qd35-alarm-weekday-wake-prepare.service"
    prepare_timer = tmp_path / "units/qd35-alarm-weekday-wake-prepare.timer"
    assert "--prepare" in prepare_service.read_text(encoding="utf-8")
    assert f'--unit-dir "{timers.unit_dir}"' in prepare_service.read_text(encoding="utf-8")
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
    cutoff = datetime(2026, 11, 20, 15, 30, tzinfo=UTC)
    timers.snooze("weekday-wake", 10, cutoff=cutoff)
    timer = tmp_path / "units/qd35-alarm-weekday-wake-snooze.timer"
    text = timer.read_text(encoding="utf-8")
    assert "OnActiveSec=10min" in text
    assert "Unit=qd35-alarm-weekday-wake-snooze.service" in text
    assert ("restart", "qd35-alarm-weekday-wake-snooze.timer") in calls
    reloaded = SystemdUserTimers(timers.unit_dir, timers.state_path)
    assert reloaded.snooze_cutoff("weekday-wake") == cutoff


@pytest.mark.parametrize("minute", [7, 20])
async def test_mcp_snooze_respects_original_nine_thirty_cutoff(tmp_path, monkeypatch, minute):
    now = datetime(2026, 11, 20, 15, minute, 32, tzinfo=UTC)
    cutoff = datetime(2026, 11, 20, 15, 30, tzinfo=UTC)
    monkeypatch.setattr(alarm_module, "datetime", SimpleNamespace(now=lambda _tz: now))
    plan = sample_alarm(wake_time="09:00", audio_source="/radio.mp3", custom_display=False)
    store = AlarmStore(tmp_path / "alarms.json")
    store.save({plan.alarm_id: plan})
    config = fake_config()
    timers = FakeTimers()
    timers.active = True
    manager = AlarmManager(QD35Service(FakeTransport(), config), config, store, timers)

    result = await manager.snooze(plan.alarm_id, 10)

    assert result["cutoff"] == cutoff.isoformat()
    assert timers.stopped == [plan.alarm_id]
    if minute == 7:
        assert result["snoozed"] is True
        assert timers.snooze_cutoff(plan.alarm_id) == cutoff
        assert timers.snoozed == [(plan.alarm_id, 10)]
    else:
        assert result["snoozed"] is False
        assert result["resume_at"] is None
        assert result["daytime_state"]["rgb_off"] is True
        assert not timers.snoozed


async def test_idle_snooze_does_not_arm_a_timer_and_stop_restores_active_display(tmp_path):
    plan = sample_alarm()
    store = AlarmStore(tmp_path / "alarms.json")
    store.save({plan.alarm_id: plan})
    config = fake_config(custom_display_firmware=True)
    service = QD35Service(FakeTransport(), config)
    timers = FakeTimers()
    manager = AlarmManager(service, config, store, timers)

    await service.set_light(effect="static", rgb="#FF6A20", brightness=100)
    assert (await manager.snooze(plan.alarm_id))["reason"] == "alarm is idle"
    assert not timers.snoozed and not timers.stopped

    timers.active = True
    result = await manager.stop(plan.alarm_id)
    assert result["daytime_state"] == {"rgb_off": True, "display_reset_acknowledged": True}
    assert timers.stopped == [plan.alarm_id]


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_alarm_updates_serialize_across_managers(tmp_path, cancel_first):
    path = tmp_path / "alarms.json"
    config = fake_config(timezone_state_path=str(tmp_path / "timezone"))
    install_started = threading.Event()
    finish_install = threading.Event()

    class DelayedTimers(FakeTimers):
        def install(self, alarm):
            install_started.set()
            assert finish_install.wait(3)
            super().install(alarm)

    first = AlarmManager(QD35Service(FakeTransport(), config), config,
                         AlarmStore(path), DelayedTimers())
    second = AlarmManager(QD35Service(FakeTransport(), config), config,
                          AlarmStore(path), FakeTimers())
    plan = sample_alarm()
    other = replace(plan, alarm_id="weekend")
    a = asyncio.create_task(first.store.edit(lambda: first._install_plan(plan)))
    assert await asyncio.to_thread(install_started.wait, 2)
    if cancel_first:
        a.cancel()
    b = asyncio.create_task(second.store.edit(lambda: second._install_plan(other)))
    try:
        await asyncio.sleep(0.1)
        assert set(first.store.load()) == {plan.alarm_id}
        assert not b.done()
    finally:
        finish_install.set()
        results = await asyncio.gather(a, b, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError) if cancel_first else results[0] is None
    assert results[1] is None
    assert set(first.store.load()) == {plan.alarm_id, other.alarm_id}


def test_systemd_state_reports_active_snooze_playback(tmp_path, monkeypatch) -> None:
    timers = SystemdUserTimers(tmp_path, tmp_path / "alarms.json")

    def systemctl(*arguments, **_kwargs):
        playing = arguments == ("is-active", "qd35-alarm-weekday-wake-snooze.service")
        return SimpleNamespace(stdout="active\n" if playing else "")

    monkeypatch.setattr(timers, "_run", systemctl)
    state = timers.state("weekday-wake")
    assert state["service_active"] is True
    assert state["snooze_service_active"] is True
    assert state["snooze_active"] is False


def test_physical_snooze_schedules_without_stopping_its_own_service(
    tmp_path, monkeypatch
) -> None:
    timers = SystemdUserTimers(tmp_path / "units", tmp_path / "alarms.json")
    (tmp_path / "units").mkdir()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(timers, "_run", lambda *args, **_kwargs: calls.append(args))

    timers.schedule_snooze("weekday-wake", 10)

    assert not any(call[:1] == ("stop",) for call in calls)
    assert ("restart", "qd35-alarm-weekday-wake-snooze.timer") in calls


def test_install_removes_obsolete_bedtime_units_and_uses_safe_descriptions(tmp_path, monkeypatch):
    timers = SystemdUserTimers(tmp_path, tmp_path / "alarms.json")
    calls = []
    monkeypatch.setattr(timers, "_run", lambda *args, **kwargs: calls.append(args))
    timers.install(sample_alarm(name="Weekday Wake\nSecond line"))
    assert "Second line" not in (tmp_path / "qd35-alarm-weekday-wake.service").read_text()
    timers.install(sample_alarm(bedtime=None))
    assert ("disable", "--now", "qd35-alarm-weekday-wake-prepare.timer") in calls
    assert not list(tmp_path.glob("*-prepare.*"))


def test_new_alarm_can_install_keepalive_without_connecting(tmp_path, monkeypatch):
    timers = SystemdUserTimers(tmp_path, tmp_path / "alarms.json")
    calls = []
    monkeypatch.setattr(timers, "_run", lambda *args, **kwargs: calls.append(args))
    timers.install_keepalive("AA:BB:CC:DD:EE:FF", start=False)
    assert (tmp_path / "qd35-alarm-audio-keepalive.service").exists()
    assert calls == [("daemon-reload",)]


async def test_alarm_creation_respects_write_gate_and_reserved_names(tmp_path):
    config = fake_config(writes_enabled=False, alarm_state_path=str(tmp_path / "alarms.json"))
    service = QD35Service(FakeTransport(), config)
    manager = AlarmManager(service, config, timers=FakeTimers())
    args = ("07:00", None, "America/Regina", ["mon"], 30, 0, 100, "#FF6A20", "12h", None, 10, 4)
    with pytest.raises(WritesDisabled):
        await manager.create("Weekday Wake", *args)
    manager = AlarmManager(service, replace(config, writes_enabled=True), timers=FakeTimers())
    with pytest.raises(ValueError, match="reserved"):
        await manager.create("Weekday Wake Snooze", *args)


async def test_failed_alarm_update_restores_previous_timer_and_state(tmp_path):
    class FailingTimers(FakeTimers):
        def install(self, alarm):
            if alarm.wake_time == "08:00":
                raise RuntimeError("timer installation failed")
            super().install(alarm)

    config = fake_config(timezone_state_path=str(tmp_path / "timezone.json"))
    service = QD35Service(FakeTransport(), config)
    store = AlarmStore(tmp_path / "alarms.json")
    timers = FailingTimers()
    manager = AlarmManager(service, config, store=store, timers=timers)
    old = sample_alarm()
    store.save({old.alarm_id: old})
    args = ("08:00", None, "America/Regina", ["mon"], 30, 0, 100, "#FF6A20", "12h", None, 10, 4)
    with pytest.raises(RuntimeError, match="timer installation failed"):
        await manager.create(old.name, *args)
    assert timers.installed == [old]
    assert store.load() == {old.alarm_id: old}


async def test_fade_out_minutes_defaults_persists_and_validates(tmp_path) -> None:
    raw = sample_alarm().to_dict()
    raw.pop("fade_out_minutes", None)
    loaded = SunriseAlarm.from_dict(raw)
    assert loaded.fade_out_minutes == 2

    config = fake_config(
        alarm_state_path=str(tmp_path / "alarms.json"),
        timezone_state_path=str(tmp_path / "timezone.json"),
    )
    store = AlarmStore(tmp_path / "alarms.json")
    timers = FakeTimers()
    manager = AlarmManager(
        QD35Service(FakeTransport(), config),
        config,
        store=store,
        timers=timers,  # type: ignore[arg-type]
    )
    args = ("07:00", None, "America/Regina", ["mon"], 30, 0, 100, "#FF6A20", "12h", None, 10, 4)
    for bad_value in (-1, 61):
        with pytest.raises(ValueError, match="fade_out_minutes"):
            await manager.create("Custom Fade", *args, fade_out_minutes=bad_value)

    created = await manager.create("Custom Fade", *args, fade_out_minutes=5)
    assert created["fade_out_minutes"] == 5
    assert store.load()["custom-fade"].fade_out_minutes == 5
    zeroed = await manager.create("Zero Fade", *args, post_wake_minutes=0, fade_out_minutes=2)
    assert zeroed["fade_out_minutes"] == 2
