from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qd35_mcp import alarm_runner
from qd35_mcp.alarms import SunriseAlarm
from qd35_mcp.errors import ProtocolError


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
        fade_out_minutes=0,
    )
    return replace(value, **changes)


class FakeService:
    alarm_control_connected = True

    def __init__(self) -> None:
        self.clock_formats: list[str] = []
        self.inputs: list[str] = []
        self.lights: list[dict[str, object]] = []
        self.volumes: list[int] = []
        self.displays: list[dict[str, object]] = []
        self.disconnected = False
        self.display_resets = 0
        self.event_calls = 0
        self.emit_manual_control = False
        self.manual_volume = 4

    async def status(self) -> dict[str, object]:
        return {
            "volume": {"maximum": 16, "current": 8},
            "input": {"group": 2, "value": 1, "name": "bluetooth"},
            "eq_preset": {"value": 0, "name": "music"},
            "lighting": {
                "group": 1,
                "selected_effect_id": 6,
                "effects": [
                    {
                        "id": 6,
                        "raw_color": [255, 106, 32],
                        "brightness": 80,
                        "wire_speed": 255,
                    }
                ],
            },
            "playback": {"value": 3, "playing": False},
        }

    async def alarm_control(self, field: str) -> object:
        if field == "volume" and self.emit_manual_control and self.event_calls >= 2:
            return {"current": self.manual_volume}
        return (await self.status())[field]

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

    async def reset_display(self) -> dict[str, object]:
        self.display_resets += 1
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
                        "parsed": {"maximum": 16, "current": self.manual_volume},
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


async def test_duplicate_control_state_does_not_snooze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService()
    service.emit_manual_control = True
    service.manual_volume = 8
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    clock = iter((0, 0, 0, 60, 60))
    monkeypatch.setattr(alarm_runner, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    result = await alarm_runner.run_alarm(alarm(sunrise_minutes=1), tick_seconds=0.01)

    assert result["completed"] is True
    assert "snoozed_by_physical_control" not in result
    assert service.event_calls >= 2


def test_display_patch_ack_is_not_manual_control() -> None:
    event = {
        "opcode": alarm_runner.c.SET_LIGHT,
        "payload_hex": "0100d00f000035",
        "parse_error": "lighting brightness exceeds 100",
    }
    assert alarm_runner._manual_control_changed(event, {}) is False


async def test_delayed_light_event_requires_fresh_confirmation() -> None:
    service = FakeService()
    expected = {
        "lighting": {
            "group": 1,
            "selected_effect_id": 6,
            "effects": [
                {
                    "id": 6,
                    "raw_color": [255, 106, 32],
                    "brightness": 20,
                    "wire_speed": 255,
                }
            ],
        }
    }
    event = {
        "sequence": 1,
        "opcode": alarm_runner.c.SET_LIGHT,
        "payload_hex": "0106ff6a200aff",
        "parsed": {
            "group": 1,
            "effect_id": 6,
            "raw_color": [255, 106, 32],
            "brightness": 10,
            "wire_speed": 255,
        },
    }

    async def events(since_sequence: int, limit: int) -> dict[str, object]:
        return {"latest_sequence": 1, "events": [event] if since_sequence == 0 else []}

    current = expected["lighting"]

    async def read_light(field: str) -> dict[str, object]:
        assert field == "lighting"
        return current

    service.events = events
    service.alarm_control = read_light
    assert await alarm_runner._wait_for_manual_control(service, 0, 0.01, expected) == (1, None)
    current = {
        **expected["lighting"],
        "effects": [{**expected["lighting"]["effects"][0], "brightness": 10}],
    }
    assert await alarm_runner._wait_for_manual_control(service, 0, 0.01, expected) == (1, event)


class FakePlayer:
    pid = 4242

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.process = self
        self.writer = self
        self.gains: list[int] = []

    def close(self) -> None:
        pass

    async def set_gain(self, gain: int) -> None:
        self.gains.append(gain)

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
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _address: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _alarm: _async_value(player))

    async def release_audio(_address):
        assert player.returncode == 0
        assert [step["brightness"] for step in service.lights] == [80, 0]
        assert service.display_resets == 1
        return release

    monkeypatch.setattr(alarm_runner, "_release_classic_audio", release_audio)

    result = await alarm_runner.run_alarm(
        alarm(audio_source="https://radio.example/stream", post_wake_minutes=0)
    )

    assert result["audio_started"] is True
    assert result["bluetooth_disconnected"] is True
    assert result["classic_release"] == release
    assert result["daytime_state"] == {"rgb_off": True, "display_reset_acknowledged": True}
    assert service.inputs == ["bluetooth"]


@pytest.mark.parametrize("dial", ["lighting", "volume"])
async def test_dials_snooze_during_post_wake_playback(monkeypatch, dial: str) -> None:
    dimmed_effect = {"raw_color": [255, 106, 32], "brightness": 70, "wire_speed": 255}

    class Playing(FakePlayer):
        async def wait(self) -> int:
            if self.returncode is None:
                await asyncio.Future()
            return await super().wait()

    class DialService(FakeService):
        async def set_light(self, **values: object) -> dict[str, object]:
            result = await super().set_light(**values)
            result["lighting"] = (await self.status())["lighting"]
            return result

        async def alarm_control(self, field: str) -> object:
            if dial == field == "lighting" and self.event_calls >= 2:
                return {
                    "group": 1, "selected_effect_id": 6,
                    "effects": [{"id": 6, **dimmed_effect}],
                }
            return await super().alarm_control(field)

        async def events(self, since_sequence: int = 0, limit: int = 100) -> dict[str, object]:
            result = await super().events(since_sequence, limit)
            if dial != "lighting" or self.event_calls < 2:
                return result
            event = {
                "sequence": 1,
                "opcode": alarm_runner.c.SET_LIGHT,
                "payload_hex": "0106ff6a2046ff",
                "parsed": {"effect_id": 6, **dimmed_effect},
            }
            return {"latest_sequence": 1, "events": [event] if since_sequence == 0 else []}

    service = DialService()
    service.emit_manual_control = dial == "volume"
    player = Playing()
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(player))

    async def unexpected_release(_address: str) -> None:
        pytest.fail("Snoozing must keep Classic audio connected")

    monkeypatch.setattr(alarm_runner, "_release_classic_audio", unexpected_release)
    result = await asyncio.wait_for(alarm_runner.run_alarm(alarm(
        audio_source="/music.wav", post_wake_minutes=30, physical_snooze_minutes=10,
    )), timeout=2)

    assert result["snoozed_by_physical_control"] is True
    assert result["snooze_minutes"] == 10
    assert [step["brightness"] for step in service.lights] == [80, 0]
    assert service.displays[-1]["power"] is False
    assert result["bluetooth_disconnected"] is False
    assert player.returncode == 0


async def test_cutoff_stops_player_before_restoring_speaker(monkeypatch) -> None:
    service = FakeService()
    process = await asyncio.create_subprocess_exec("sleep", "30")
    player = FakePlayer()
    player.process = process
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(player))
    monkeypatch.setattr(alarm_runner, "_release_classic_audio", lambda _: _async_value({
        "disconnected": True,
    }))
    restore = alarm_runner.restore_daytime_state
    restored_at = []

    async def restore_after_audio_stops(service, plan):
        assert process.returncode is not None
        restored_at.append(datetime.now(UTC))
        return await restore(service, plan)

    monkeypatch.setattr(alarm_runner, "restore_daytime_state", restore_after_audio_stops)
    wake = datetime.now(UTC)
    cutoff = wake + timedelta(seconds=0.25)
    try:
        result = await asyncio.wait_for(alarm_runner.run_alarm(
            alarm(audio_source="/music.wav"), scheduled_window=(wake, wake), cutoff=cutoff,
        ), timeout=2)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    assert result["completed"] is True
    assert player.gains == [50]
    assert restored_at[0] >= cutoff
    assert [step["brightness"] for step in service.lights] == [80, 0]
    assert service.display_resets == 1


async def test_cutoff_stops_player_during_a_stalled_ble_read(monkeypatch) -> None:
    service, player = FakeService(), FakePlayer()
    player.process = process = await asyncio.create_subprocess_exec("sleep", "30")
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def stalled_control(_service, cursor, *_args):
        entered.set()
        await resume.wait()
        return cursor, None

    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(player))
    monkeypatch.setattr(alarm_runner, "_wait_for_manual_control", stalled_control)
    monkeypatch.setattr(alarm_runner, "_release_classic_audio", lambda _: _async_value({
        "disconnected": True,
    }))
    now = datetime.now(UTC)
    cutoff = now + timedelta(seconds=0.3)
    run = asyncio.create_task(alarm_runner.run_alarm(
        alarm(audio_source="/music.wav", audio_fade_minutes=2),
        scheduled_window=(now - timedelta(minutes=1), cutoff), cutoff=cutoff,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert any(player.gains)
        await asyncio.wait_for(process.wait(), 1)
        assert datetime.now(UTC) >= cutoff
        assert not run.done()
    finally:
        resume.set()
        await asyncio.wait_for(run, 2)


async def test_post_wake_playback_recovers_after_mpv_exit(monkeypatch) -> None:
    service = FakeService()
    started = [FakePlayer(), FakePlayer()]
    started[1].process = process = await asyncio.create_subprocess_exec("sleep", "30")
    players = iter(started)
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(next(players)))
    monkeypatch.setattr(alarm_runner, "_release_classic_audio", lambda _: _async_value({
        "disconnected": True,
    }))
    wake = datetime.now(UTC)
    try:
        result = await asyncio.wait_for(alarm_runner.run_alarm(
            alarm(audio_source="/radio"), scheduled_window=(wake, wake),
            cutoff=wake + timedelta(seconds=0.35),
        ), 2)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert result["completed"] is True
    assert [player.gains for player in started] == [[50], [50]]
    assert result["player_pid"] == process.pid


async def test_disconnected_dial_listener_reinitializes_and_accepts_new_events() -> None:
    class DroppedLink(FakeService):
        alarm_control_connected = False

        async def reconnect_alarm_controls(self):
            self.alarm_control_connected = True
            self.emit_manual_control = True
            self.event_calls = 1
            return {"current": 8}

    service = DroppedLink()
    cursor, event = await alarm_runner._wait_for_manual_control(
        service, 0, 0.3, {"volume": {"current": 8}},
    )
    assert service.alarm_control_connected
    assert cursor == 1 and event["parsed"]["current"] == 4


async def test_audio_fade_raises_software_gain_without_repeated_speaker_writes(monkeypatch) -> None:
    service = FakeService()
    player = FakePlayer()
    elapsed = 0
    monkeypatch.setattr(alarm_runner, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(player))
    monkeypatch.setattr(alarm_runner, "_release_classic_audio", lambda _: _async_value({
        "disconnected": True,
    }))

    async def no_manual_control(_service, cursor, *_args):
        nonlocal elapsed
        elapsed += 20
        return cursor, None

    monkeypatch.setattr(alarm_runner, "_wait_for_manual_control", no_manual_control)
    result = await alarm_runner.run_alarm(alarm(
        audio_source="/music.wav", sunrise_minutes=1, audio_fade_minutes=1,
        target_speaker_volume=5, post_wake_minutes=0,
    ))

    assert result["completed"] is True
    assert result["speaker_volume"] == 5
    assert service.volumes == [5]
    assert player.gains == [17, 33, 50]


@pytest.mark.parametrize("startup_delay", [0, 3])
@pytest.mark.parametrize("snoozed", [False, True])
async def test_late_alarm_keeps_original_audio_cutoff(monkeypatch, startup_delay, snoozed) -> None:
    service = FakeService()
    player = FakePlayer()
    start = datetime(2026, 11, 18, 14, 30, tzinfo=UTC)
    wake = datetime(2026, 11, 18, 15, tzinfo=UTC)
    cutoff = None
    if snoozed:
        start = datetime(2026, 11, 18, 15, 17, 33, tzinfo=UTC)
        wake = datetime(2026, 11, 18, 15, 19, 33, tzinfo=UTC)
        cutoff = datetime(2026, 11, 18, 15, 30, tzinfo=UTC)
    now = datetime(2026, 11, 18, 15, 29, 58, tzinfo=UTC).timestamp()
    monkeypatch.setattr(alarm_runner, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _address: _async_value(None))

    async def start_audio(_alarm):
        nonlocal now
        now += startup_delay
        return player

    monkeypatch.setattr(alarm_runner, "_start_audio", start_audio)
    monkeypatch.setattr(
        alarm_runner,
        "_release_classic_audio",
        lambda _address: _async_value({"disconnected": True}),
    )
    waits = []

    async def reached_cutoff(_service, _player, _cursor, _expected, clock, wake_end):
        nonlocal now
        if wake_end > clock():
            waits.append(wake_end - clock())
            now = wake_end
        return None

    monkeypatch.setattr(alarm_runner, "_wait_after_fade", reached_cutoff)
    result = await alarm_runner.run_alarm(
        alarm(audio_source="/music.wav", sunrise_minutes=30, target_speaker_volume=4),
        scheduled_window=(start, wake),
        cutoff=cutoff,
    )
    if startup_delay:
        assert waits == []
        assert not result["completed"]
        assert service.volumes == []
        assert player.gains == []
    else:
        assert waits == [2]
        assert result["completed"]
        assert service.volumes == [4]
        assert player.gains == [50]


@pytest.mark.parametrize("prepare", [True, False])
def test_expired_timer_does_not_touch_speaker(monkeypatch, prepare, capsys) -> None:
    plan = alarm(sunrise_minutes=30, wake_time="09:00", weekdays=("wed",))

    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 18, 16, tzinfo=UTC)

    def unexpected_call(*args, **kwargs):
        raise AssertionError("an expired timer must not run Bluetooth work")

    monkeypatch.setattr(alarm_runner, "datetime", Morning)
    monkeypatch.setattr(
        alarm_runner, "AlarmStore", lambda _: SimpleNamespace(load=lambda: {"test": plan})
    )
    monkeypatch.setattr(alarm_runner, "run_alarm", unexpected_call)
    monkeypatch.setattr(alarm_runner, "prepare_overnight", unexpected_call)
    monkeypatch.setattr(alarm_runner, "_parse_args", lambda: SimpleNamespace(
        alarm="test", state=Path("alarms.json"), unit_dir=None, prepare=prepare, snoozed=False,
    ))
    trigger = "qd35-alarm-test-prepare.timer" if prepare else "qd35-alarm-test.timer"
    monkeypatch.setenv("TRIGGER_UNIT", trigger)
    alarm_runner.main()
    assert "window has passed" in capsys.readouterr().err


@pytest.mark.parametrize("expired", [False, True])
def test_snooze_cli_uses_cutoff_persisted_by_its_timer(tmp_path, monkeypatch, expired, capsys):
    plan = alarm(wake_time="09:00", audio_source="/radio.mp3")
    cutoff = datetime(2026, 11, 20, 15, 30, tzinfo=UTC)
    resumed = cutoff if expired else datetime(2026, 11, 20, 15, 17, 33, tzinfo=UTC)
    monkeypatch.setattr(alarm_runner, "datetime", SimpleNamespace(now=lambda _tz: resumed))
    state = tmp_path / "alarms.json"
    alarm_runner.AlarmStore(state).save({plan.alarm_id: plan})
    timers = alarm_runner.SystemdUserTimers(tmp_path, state)
    monkeypatch.setattr(timers, "_run", lambda *_: None)
    timers.schedule_snooze(plan.alarm_id, 10, cutoff=cutoff)
    monkeypatch.setattr(alarm_runner, "_parse_args", lambda: SimpleNamespace(
        alarm=plan.alarm_id, state=state, unit_dir=tmp_path, prepare=False, snoozed=True,
    ))
    calls = []

    async def run_snooze(updated, **options):
        calls.append((updated.start_brightness, options))
        return {"completed": True}

    monkeypatch.setattr(alarm_runner, "_run_owned", run_snooze)
    monkeypatch.setattr(alarm_runner, "_finish_missed_snooze", lambda _: _async_value({}))
    alarm_runner.main()

    if expired:
        assert not calls
        assert "snoozed wake window has passed" in capsys.readouterr().err
    else:
        assert calls == [(0, {
            "scheduled_window": (resumed, resumed + timedelta(minutes=2)), "cutoff": cutoff,
        })]


async def test_failed_clock_and_lighting_do_not_prevent_audio_or_release(monkeypatch) -> None:
    class FailingLights(FakeService):
        async def sync_clock(self, *args):
            raise ProtocolError("clock query failed")

        async def set_light(self, **values):
            return {"applied": False}

    service = FailingLights()
    player = FakePlayer()
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _address: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _alarm: _async_value(player))
    monkeypatch.setattr(
        alarm_runner,
        "_release_classic_audio",
        lambda _address: _async_value({"disconnected": True}),
    )
    result = await alarm_runner.run_alarm(alarm(audio_source="/music.wav", post_wake_minutes=0))

    assert result["audio_started"] and result["bluetooth_disconnected"]
    assert not result["completed"]
    assert set(result["errors"]) == {"clock", "lighting", "daytime_state"}
    assert result["daytime_state"]["rgb_off"] is False
    assert result["daytime_state"]["display_reset_acknowledged"] is True


async def test_player_exit_during_fade_is_retried_and_reported(monkeypatch) -> None:
    service = FakeService()
    player = FakePlayer()
    attempts: list[int] = []
    clock = iter((0, 0, 0, 60, 60))
    monkeypatch.setattr(alarm_runner, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _address: _async_value(None))
    monkeypatch.setattr(
        alarm_runner,
        "_release_classic_audio",
        lambda _address: _async_value({"disconnected": True}),
    )

    async def start(_alarm):
        attempts.append(1)
        if len(attempts) > 1:
            raise RuntimeError("radio unavailable")
        return player

    async def idle(_service, cursor, *_args):
        player.returncode = 2
        return cursor, None

    monkeypatch.setattr(alarm_runner, "_start_audio", start)
    monkeypatch.setattr(alarm_runner, "_wait_for_manual_control", idle)
    result = await alarm_runner.run_alarm(
        alarm(sunrise_minutes=1, audio_fade_minutes=1, audio_source="/music.wav")
    )

    assert len(attempts) == 3
    assert not result["completed"]
    assert result["errors"]["audio"] == "radio unavailable"
    assert result["bluetooth_disconnected"]
    assert service.volumes == []


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


async def test_bedtime_still_blanks_display_when_audio_and_rgb_fail(monkeypatch) -> None:
    class FailedRGB(FakeService):
        async def set_light(self, **_values):
            raise ProtocolError("no RGB acknowledgement")

    service = FailedRGB()
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_set_keepalive_enabled", lambda _: _async_value({
        "applied": True,
    }))
    monkeypatch.setattr(alarm_runner, "_wait_for_classic_connection", lambda _: _async_value(False))
    result = await alarm_runner.prepare_overnight(alarm(audio_source="/radio"))
    assert result["prepared"] is False
    assert set(result["errors"]) == {"audio", "lighting"}
    assert service.displays[-1]["power"] is False


async def test_bedtime_retries_after_adapter_recovers(monkeypatch) -> None:
    attempts = []
    sleeps = []

    async def prepare(_alarm, *, prepare=False):
        assert prepare
        attempts.append(1)
        return {"prepared": len(attempts) == 2}

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(alarm_runner, "_run_owned", prepare)
    monkeypatch.setattr(alarm_runner.asyncio, "sleep", sleep)
    deadline = datetime.now(UTC) + timedelta(minutes=10)
    assert await alarm_runner._retry_bedtime(alarm(), deadline) == {"prepared": True}
    assert sleeps == [30]


async def test_start_audio_selects_exact_native_pipewire_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = FakePlayer()
    player.stdout.feed_data(b"a log message\nQD35_ALARM_AUDIO_READY 44100\n")
    player.stdout.feed_eof()
    command: list[str] = []

    async def sink(_address: str) -> str:
        return "bluez_output.AA_BB_CC_DD_EE_FF.1"

    async def launch(*arguments: str, **_kwargs: object) -> FakePlayer:
        command.extend(arguments)
        return player

    monkeypatch.setattr(alarm_runner, "_find_audio_sink", sink)
    monkeypatch.setattr(alarm_runner, "_confirm_audio_route", lambda *_: _async_value(None))
    monkeypatch.setattr(alarm_runner.asyncio, "create_subprocess_exec", launch)

    started = await alarm_runner._start_audio(
        alarm(audio_source="https://radio.example/stream")
    )

    assert started.process is player
    assert "--no-config" in command
    assert "--msg-level=all=info" in command
    assert "--audio-device=pipewire/bluez_output.AA_BB_CC_DD_EE_FF.1" in command
    assert any(argument.startswith("--input-ipc-client=fd://") for argument in command)
    assert "--volume=0" in command
    assert command[-2:] == ["--", "https://radio.example/stream"]
    await alarm_runner._stop_player(started)


async def test_audio_route_verifies_actual_sink_for_its_own_stream(monkeypatch) -> None:
    stream_sink = 2
    truncated = True

    async def command(*args, **kwargs):
        nonlocal truncated
        if args[-1] == "sinks":
            if truncated:
                truncated = False
                return 0, '[{"index":'
            return 0, '[{"index":1,"name":"qd35"},{"index":2,"name":"hdmi"}]'
        return 0, json.dumps([{"sink": stream_sink, "properties": {"node.name": "qd35-test"}}])

    monkeypatch.setattr(alarm_runner, "_run_command", command)
    monkeypatch.setattr(alarm_runner.asyncio, "sleep", lambda _: _async_value(None))
    with pytest.raises(RuntimeError, match="did not link"):
        await alarm_runner._confirm_audio_route("qd35", "qd35-test")
    stream_sink = 1
    await alarm_runner._confirm_audio_route("qd35", "qd35-test")


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


async def test_post_wake_fades_audio_and_light_over_final_two_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EchoingLightService(FakeService):
        def __init__(self) -> None:
            super().__init__()
            self.brightness = 80
            self.recorded_events: list[dict[str, object]] = []
            self.sequence = 0

        def _lighting_payload(self) -> dict[str, object]:
            return {
                "group": 1,
                "selected_effect_id": 6,
                "effects": [
                    {
                        "id": 6,
                        "raw_color": [255, 106, 32],
                        "brightness": self.brightness,
                        "wire_speed": 255,
                    }
                ],
            }

        async def alarm_control(self, field: str) -> object:
            if field == "lighting":
                return self._lighting_payload()
            return await super().alarm_control(field)

        async def set_light(self, **values: object) -> dict[str, object]:
            result = await super().set_light(**values)
            self.brightness = int(values["brightness"])  # type: ignore[arg-type]
            lighting = self._lighting_payload()
            result["lighting"] = lighting
            self.sequence += 1
            self.recorded_events.append(
                {
                    "sequence": self.sequence,
                    "opcode": alarm_runner.c.SET_LIGHT,
                    "payload_hex": f"0106ff6a20{self.brightness:02x}ff",
                    "parsed": {
                        "effect_id": 6,
                        "raw_color": [255, 106, 32],
                        "brightness": self.brightness,
                        "wire_speed": 255,
                    },
                }
            )
            return result

        async def events(
            self, since_sequence: int = 0, limit: int = 100
        ) -> dict[str, object]:
            events = [
                event for event in self.recorded_events if int(event["sequence"]) > since_sequence
            ][:limit]
            return {
                "events": events,
                "latest_sequence": self.sequence,
                "count": len(events),
            }

    class Playing(FakePlayer):
        def __init__(self, get_now: Any) -> None:
            super().__init__()
            self._get_now = get_now
            self.gain_timeline: list[tuple[float, int]] = []

        async def set_gain(self, gain: int) -> None:
            await super().set_gain(gain)
            self.gain_timeline.append((self._get_now(), gain))

        async def wait(self) -> int:
            if self.returncode is None:
                await asyncio.Future()
            return await super().wait()

    now = 0.0
    service = EchoingLightService()
    player = Playing(lambda: now)
    light_timeline: list[tuple[float, int]] = []
    original_set_light = service.set_light

    async def tracked_set_light(**values: object) -> dict[str, object]:
        light_timeline.append((now, int(values["brightness"])))  # type: ignore[arg-type]
        return await original_set_light(**values)

    service.set_light = tracked_set_light  # type: ignore[method-assign]
    monkeypatch.setattr(alarm_runner, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(player))
    monkeypatch.setattr(
        alarm_runner, "_release_classic_audio", lambda _: _async_value({"disconnected": True})
    )

    async def poll_events(svc, cursor, timeout, expected):
        nonlocal now
        res = await svc.events(since_sequence=cursor, limit=100)
        for raw_event in res.get("events", []):
            event = dict(raw_event)
            cursor = int(event.get("sequence", cursor))
            if alarm_runner._manual_control_changed(event, expected):
                return cursor, event
        cursor = int(res.get("latest_sequence", cursor))
        now += timeout
        return cursor, None

    monkeypatch.setattr(alarm_runner, "_wait_for_manual_control", poll_events)

    result = await alarm_runner.run_alarm(
        alarm(
            audio_source="https://radio.example/stream",
            target_speaker_volume=4,
            post_wake_minutes=30,
            fade_out_minutes=2,
        )
    )

    assert result["completed"] is True
    assert result["fade_out_minutes"] == 2
    assert result["bluetooth_disconnected"] is True
    assert "snoozed_by_physical_control" not in result
    assert service.volumes == [4]
    assert service.display_resets == 1
    assert player.gains[0] == 50
    fade_gains = [item for item in player.gain_timeline if item[0] > 0]
    assert all(timestamp >= 1680.0 for timestamp, _ in fade_gains)
    assert fade_gains[-1][1] <= 1
    assert [gain for _, gain in fade_gains] == sorted(
        (gain for _, gain in fade_gains), reverse=True
    )
    fade_lights = [item for item in light_timeline if 0 < item[0] < 1800]
    assert fade_lights and all(timestamp >= 1680.0 for timestamp, _ in fade_lights)
    assert fade_lights[-1][1] == 0
    assert [level for _, level in fade_lights] == sorted(
        (level for _, level in fade_lights), reverse=True
    )
    assert light_timeline[-1] == (1800.0, 0)


async def test_physical_control_during_fade_out_restores_daytime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Playing(FakePlayer):
        async def wait(self) -> int:
            if self.returncode is None:
                await asyncio.Future()
            return await super().wait()

    service = FakeService()
    player = Playing()
    wake = datetime.now(UTC)
    cutoff = wake + timedelta(minutes=5)
    now = wake.timestamp()
    monkeypatch.setattr(alarm_runner, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(player))
    monkeypatch.setattr(
        alarm_runner, "_release_classic_audio", lambda _: _async_value({"disconnected": True})
    )

    async def dial_during_fade(_service, cursor, timeout, _expected):
        nonlocal now
        if now >= cutoff.timestamp() - 60:
            return cursor + 1, {
                "sequence": cursor + 1,
                "opcode": alarm_runner.c.QUERY_VOLUME,
                "parsed": {"maximum": 16, "current": 4},
            }
        now += timeout
        return cursor, None

    monkeypatch.setattr(alarm_runner, "_wait_for_manual_control", dial_during_fade)
    result = await alarm_runner.run_alarm(
        alarm(
            audio_source="https://radio.example/stream",
            post_wake_minutes=5,
            fade_out_minutes=2,
            physical_snooze_minutes=10,
        ),
        scheduled_window=(wake, wake),
        cutoff=cutoff,
    )

    assert result["completed"] is True
    assert result["snooze_skipped"] == "snooze would resume at or after the wake cutoff"
    assert service.display_resets == 1
    assert service.lights[-1]["brightness"] == 0
    assert result["bluetooth_disconnected"] is True
    assert player.returncode == 0


async def test_mpv_recovery_during_fade_out_resumes_at_faded_gain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0

    class FirstPlayer(FakePlayer):
        async def wait(self) -> int:
            if now >= 180.0:
                self.returncode = 2
                return 2
            await asyncio.sleep(0)
            return await self.wait()

    class SecondPlayer(FakePlayer):
        async def wait(self) -> int:
            if self.returncode is None:
                await asyncio.Future()
            return await super().wait()

    service = FakeService()
    started = [FirstPlayer(), SecondPlayer()]
    players = iter(started)
    monkeypatch.setattr(alarm_runner, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(next(players)))
    monkeypatch.setattr(
        alarm_runner, "_release_classic_audio", lambda _: _async_value({"disconnected": True})
    )

    async def advance(_service, cursor, timeout, _expected):
        nonlocal now
        now += timeout
        return cursor, None

    monkeypatch.setattr(alarm_runner, "_wait_for_manual_control", advance)
    result = await alarm_runner.run_alarm(
        alarm(
            audio_source="https://radio.example/stream",
            post_wake_minutes=4,
            fade_out_minutes=2,
        )
    )

    assert result["completed"] is True
    assert started[0].gains[0] == 50
    assert started[1].gains[0] == 25
    assert max(started[1].gains) <= 25
    assert started[1].gains[-1] <= 1


async def test_ble_light_failure_during_fade_out_does_not_block_audio_fade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0

    class FlakyLightService(FakeService):
        async def set_light(self, **values: object) -> dict[str, object]:
            if 0 < now < 180:
                raise ProtocolError("ble light failed")
            return await super().set_light(**values)

    class Playing(FakePlayer):
        async def wait(self) -> int:
            if self.returncode is None:
                await asyncio.Future()
            return await super().wait()

    service = FlakyLightService()
    player = Playing()
    monkeypatch.setattr(alarm_runner, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(player))
    monkeypatch.setattr(
        alarm_runner, "_release_classic_audio", lambda _: _async_value({"disconnected": True})
    )

    async def advance(_service, cursor, timeout, _expected):
        nonlocal now
        now += timeout
        return cursor, None

    monkeypatch.setattr(alarm_runner, "_wait_for_manual_control", advance)
    result = await alarm_runner.run_alarm(
        alarm(
            audio_source="https://radio.example/stream",
            post_wake_minutes=3,
            fade_out_minutes=2,
        )
    )

    assert result["completed"] is False
    assert result["errors"]["lighting"] == "ble light failed"
    assert player.gains[0] == 50
    assert player.gains[-1] <= 1
    assert player.returncode == 0
    assert result["bluetooth_disconnected"] is True


async def test_short_remaining_window_clamps_fade_out_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Playing(FakePlayer):
        def __init__(self, get_now: Any) -> None:
            super().__init__()
            self._get_now = get_now
            self.gain_timeline: list[tuple[float, int]] = []

        async def set_gain(self, gain: int) -> None:
            await super().set_gain(gain)
            self.gain_timeline.append((self._get_now(), gain))

        async def wait(self) -> int:
            if self.returncode is None:
                await asyncio.Future()
            return await super().wait()

    start = datetime(2026, 11, 20, 15, 29, tzinfo=UTC)
    cutoff = start + timedelta(seconds=60)
    now = start.timestamp()
    service = FakeService()
    player = Playing(lambda: now)
    monkeypatch.setattr(alarm_runner, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(alarm_runner, "create_service", lambda: service)
    monkeypatch.setattr(alarm_runner, "_connect_classic", lambda _: _async_value(None))
    monkeypatch.setattr(alarm_runner, "_start_audio", lambda _: _async_value(player))
    monkeypatch.setattr(
        alarm_runner, "_release_classic_audio", lambda _: _async_value({"disconnected": True})
    )

    async def advance(_service, cursor, timeout, _expected):
        nonlocal now
        now += timeout
        return cursor, None

    monkeypatch.setattr(alarm_runner, "_wait_for_manual_control", advance)
    result = await alarm_runner.run_alarm(
        alarm(
            audio_source="https://radio.example/stream",
            post_wake_minutes=30,
            fade_out_minutes=2,
        ),
        scheduled_window=(start, start),
        cutoff=cutoff,
    )

    assert result["completed"] is True
    midpoint = next(
        gain for timestamp, gain in player.gain_timeline if timestamp == start.timestamp() + 30
    )
    assert midpoint == 25
    assert player.gains[-1] == 2
    assert service.lights[1]["brightness"] < 80
    assert service.lights[-1]["brightness"] == 0


async def _async_value(value: Any) -> Any:
    return value
