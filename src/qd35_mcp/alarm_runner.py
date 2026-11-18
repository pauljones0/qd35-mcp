"""Run one persisted QD35 sunrise alarm under a systemd user service."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import constants as c
from .alarms import (
    AlarmStore,
    SunriseAlarm,
    SystemdUserTimers,
    perceptual_light_value,
    ramp_value,
)
from .display import DISPLAY_MAGIC, DISPLAY_RESET, DISPLAY_SET_STATE, DISPLAY_WRITE_GRID
from .server import create_service

_ALARM_ENVIRONMENT_KEYS = (
    "QD35_DEVICE",
    "QD35_ENABLE_WRITES",
    "QD35_CUSTOM_DISPLAY_FIRMWARE",
    "QD35_SCAN_TIMEOUT",
)
_KEEPALIVE_UNIT = "qd35-alarm-audio-keepalive.service"
_CONTROL_EVENT_OPCODES = frozenset(
    {
        c.QUERY_INPUT,
        c.SET_INPUT,
        c.QUERY_VOLUME,
        c.SET_VOLUME,
        c.QUERY_EQ_PRESET,
        c.SET_EQ_PRESET,
        c.QUERY_LIGHT,
        c.SET_LIGHT,
        c.RESET_LIGHT,
        c.QUERY_PLAYBACK,
    }
)


def _alarm_unit_dir() -> Path:
    configured = (os.getenv("QD35_ALARM_UNIT_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd" / "user"


def _restore_environment(previous: dict[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


async def _run_command(*arguments: str, timeout: float = 20) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "command timed out"
    return process.returncode or 0, output.decode("utf-8", "replace").strip()


async def _connect_classic(address: str) -> None:
    code, info = await _run_command("bluetoothctl", "info", address)
    if code == 0 and "Connected: yes" in info:
        return
    failures: list[str] = []
    for _attempt in range(3):
        code, output = await _run_command("bluetoothctl", "connect", address)
        if code == 0 and "successful" in output.lower():
            return
        failures.append(output)
        await asyncio.sleep(5)
    raise RuntimeError(
        "Classic Bluetooth audio could not connect; pair and trust the QD35 first. "
        + " | ".join(failures)
    )


async def _wait_for_classic_connection(
    address: str, timeout_seconds: float = 90
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        code, info = await _run_command("bluetoothctl", "info", address)
        if code == 0 and "Connected: yes" in info:
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(2, remaining))


async def _find_audio_sink(address: str) -> str:
    needle = address.replace(":", "_")
    for _attempt in range(20):
        code, output = await _run_command("pactl", "list", "short", "sinks")
        if code == 0:
            for line in output.splitlines():
                fields = line.split("\t")
                if len(fields) >= 2 and fields[1].startswith(f"bluez_output.{needle}"):
                    return fields[1]
        await asyncio.sleep(1)
    raise RuntimeError("QD35 connected but no PipeWire/PulseAudio sink appeared")


async def _start_audio(alarm: SunriseAlarm) -> asyncio.subprocess.Process:
    if alarm.audio_source is None:
        raise ValueError("alarm has no audio source")
    await _connect_classic(alarm.classic_device)
    sink = await _find_audio_sink(alarm.classic_device)
    process = await asyncio.create_subprocess_exec(
        "mpv",
        "--no-config",
        "--no-video",
        "--msg-level=all=warn",
        f"--audio-device=pipewire/{sink}",
        "--volume=50",
        "--loop-playlist=inf",
        "--",
        alarm.audio_source,
    )
    await asyncio.sleep(0.5)
    if process.returncode is not None:
        raise RuntimeError(f"mpv exited before alarm playback (exit {process.returncode})")
    return process


async def _stop_player(player: asyncio.subprocess.Process | None) -> int | None:
    if player is None:
        return None
    if player.returncode is None:
        player.terminate()
        try:
            await asyncio.wait_for(player.wait(), 5)
        except TimeoutError:
            player.kill()
            await player.wait()
    return player.returncode


async def _set_keepalive_enabled(enabled: bool) -> dict[str, object]:
    action = ("enable", "--now") if enabled else ("disable", "--now")
    code, output = await _run_command("systemctl", "--user", *action, _KEEPALIVE_UNIT)
    return {
        "requested": enabled,
        "applied": code == 0,
        "detail": output,
    }


async def _release_classic_audio(address: str) -> dict[str, object]:
    keepalive = await _set_keepalive_enabled(False)
    if keepalive["applied"] is not True:
        return {
            "keepalive": keepalive,
            "disconnect_exit": None,
            "disconnect_detail": "not attempted because keepalive could not be disabled",
            "disconnected": False,
        }
    code, output = await _run_command("bluetoothctl", "disconnect", address)
    lowered = output.lower()
    disconnected = code == 0 and (
        "successful" in lowered or "not connected" in lowered
    )
    return {
        "keepalive": keepalive,
        "disconnect_exit": code,
        "disconnect_detail": output,
        "disconnected": disconnected,
    }


async def _latest_event_sequence(service: Any) -> int:
    result = await service.events(limit=1)
    return int(result.get("latest_sequence", 0))


def _control_expectations(status: dict[str, object]) -> dict[str, object]:
    """Extract only state that a physical side control can observably change."""
    expected: dict[str, object] = {}
    for key in ("volume", "input", "eq_preset", "lighting", "playback"):
        value = status.get(key)
        if isinstance(value, dict) and value.get("unavailable") is not True:
            expected[key] = value
    return expected


def _remember_result(
    expected: dict[str, object], key: str, result: dict[str, object], result_key: str
) -> None:
    value = result.get(result_key)
    if isinstance(value, dict):
        expected[key] = value


def _is_display_patch_event(event: dict[str, object]) -> bool:
    """Recognize acknowledgements carried over SET_LIGHT by the optional MCU patch."""
    if event.get("opcode") != c.SET_LIGHT:
        return False
    payload_hex = event.get("payload_hex")
    if not isinstance(payload_hex, str):
        return False
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError:
        return False
    return (
        len(payload) == 7
        and payload[1] == 0
        and payload[2] in {DISPLAY_SET_STATE, DISPLAY_WRITE_GRID, DISPLAY_RESET}
        and payload[6] == DISPLAY_MAGIC
    )


def _selected_light(lighting: object) -> dict[str, object] | None:
    if not isinstance(lighting, dict):
        return None
    selected = lighting.get("selected_effect_id")
    effects = lighting.get("effects")
    if not isinstance(selected, int) or not isinstance(effects, list):
        return None
    for effect in effects:
        if isinstance(effect, dict) and effect.get("id") == selected:
            return effect
    return None


def _manual_control_changed(
    event: dict[str, object], expected: dict[str, object]
) -> bool:
    """Return true only for a parsed state change, never for a duplicate notification."""
    if _is_display_patch_event(event) or "parse_error" in event:
        return False
    parsed = event.get("parsed")
    if not isinstance(parsed, dict):
        return False
    opcode = event.get("opcode")
    if opcode == c.QUERY_VOLUME:
        current = expected.get("volume")
        return isinstance(current, dict) and parsed.get("current") != current.get("current")
    if opcode == c.QUERY_INPUT:
        current = expected.get("input")
        return isinstance(current, dict) and (
            parsed.get("group"), parsed.get("value")
        ) != (current.get("group"), current.get("value"))
    if opcode == c.QUERY_EQ_PRESET:
        current = expected.get("eq_preset")
        return isinstance(current, dict) and parsed.get("value") != current.get("value")
    if opcode in {c.QUERY_LIGHT, c.RESET_LIGHT}:
        current = expected.get("lighting")
        current_selected = _selected_light(current)
        parsed_selected = _selected_light(parsed)
        if current_selected is None or parsed_selected is None:
            return False
        return (
            parsed.get("group"),
            parsed.get("selected_effect_id"),
            parsed_selected.get("raw_color"),
            parsed_selected.get("brightness"),
            parsed_selected.get("wire_speed"),
        ) != (
            current.get("group"),  # type: ignore[union-attr]
            current.get("selected_effect_id"),  # type: ignore[union-attr]
            current_selected.get("raw_color"),
            current_selected.get("brightness"),
            current_selected.get("wire_speed"),
        )
    if opcode == c.SET_LIGHT:
        current_selected = _selected_light(expected.get("lighting"))
        if current_selected is None:
            return False
        return (
            parsed.get("effect_id"),
            parsed.get("raw_color"),
            parsed.get("brightness"),
            parsed.get("wire_speed"),
        ) != (
            current_selected.get("id"),
            current_selected.get("raw_color"),
            current_selected.get("brightness"),
            current_selected.get("wire_speed"),
        )
    if opcode == c.QUERY_PLAYBACK:
        current = expected.get("playback")
        return isinstance(current, dict) and parsed.get("value") != current.get("value")
    return False


async def _wait_for_manual_control(
    service: Any,
    since_sequence: int,
    timeout: float,
    expected: dict[str, object],
    *,
    ignore_playback_until: float = 0,
) -> tuple[int, dict[str, object] | None]:
    """Wait for a state event emitted between our own command transactions."""
    deadline = asyncio.get_running_loop().time() + timeout
    cursor = since_sequence
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return cursor, None
        await asyncio.sleep(min(0.25, remaining))
        result = await service.events(since_sequence=cursor, limit=100)
        cursor = int(result.get("latest_sequence", cursor))
        for raw_event in result.get("events", []):
            event = dict(raw_event)
            opcode = event.get("opcode")
            if opcode not in _CONTROL_EVENT_OPCODES:
                continue
            if (
                opcode == c.QUERY_PLAYBACK
                and asyncio.get_running_loop().time() < ignore_playback_until
            ):
                continue
            if _manual_control_changed(event, expected):
                return cursor, event


async def _apply_night_state(service: Any, alarm: SunriseAlarm) -> dict[str, object]:
    light = await service.set_light(
        effect="static",
        rgb=alarm.light_rgb,
        brightness=0,
    )
    if light.get("applied") is not True:
        raise RuntimeError(f"night light state was not verified: {light}")
    display: dict[str, object] | None = None
    if alarm.custom_display:
        display = await service.set_display(
            power=False,
            clock=False,
            logo=False,
            symbols=False,
            brightness=alarm.night_display_brightness,
            follow_lighting_dial=False,
            raw_mode=False,
        )
        if display.get("acknowledged") is not True:
            raise RuntimeError(f"night display state was not acknowledged: {display}")
    return {"light": light, "display": display}


async def run_alarm(alarm: SunriseAlarm, *, tick_seconds: float = 30) -> dict[str, object]:
    previous_timezone = os.environ.get("TZ")
    previous_environment = {
        name: os.environ.get(name) for name in _ALARM_ENVIRONMENT_KEYS
    }
    os.environ["TZ"] = alarm.timezone
    if hasattr(time, "tzset"):
        time.tzset()
    os.environ["QD35_DEVICE"] = alarm.ble_device
    os.environ["QD35_ENABLE_WRITES"] = "1"
    os.environ["QD35_SCAN_TIMEOUT"] = "30"
    if alarm.custom_display:
        os.environ["QD35_CUSTOM_DISPLAY_FIRMWARE"] = "1"
    service = create_service()
    player: asyncio.subprocess.Process | None = None
    audio_started = False
    last_brightness: int | None = None
    last_display_brightness: int | None = None
    last_volume: int | None = None
    duration = alarm.sunrise_minutes * 60
    audio_start = max(0, duration - alarm.audio_fade_minutes * 60)
    started = time.monotonic()
    playback_events_ignored_until = 0.0
    try:
        await service.set_timezone(alarm.timezone, sync_now=False)
        await service.sync_clock(alarm.clock_format, alarm.timezone)
        expected_controls = _control_expectations(await service.status())
        event_cursor = 0
        manual_event: dict[str, object] | None = None
        while True:
            elapsed = min(duration, time.monotonic() - started)
            light_progress = 1.0 if duration == 0 else elapsed / duration
            brightness = perceptual_light_value(
                alarm.start_brightness, alarm.end_brightness, light_progress
            )
            if brightness != last_brightness:
                result = await service.set_light(
                    effect="static", rgb=alarm.light_rgb, brightness=brightness
                )
                if result.get("applied") is not True:
                    raise RuntimeError(f"sunrise light step was not verified: {result}")
                _remember_result(expected_controls, "lighting", result, "lighting")
                last_brightness = brightness
            if alarm.custom_display:
                display_brightness = perceptual_light_value(0, 7, light_progress)
                if display_brightness != last_display_brightness:
                    display = await service.set_display(
                        power=True,
                        clock=True,
                        logo=True,
                        symbols=True,
                        brightness=display_brightness,
                        follow_lighting_dial=False,
                        raw_mode=False,
                    )
                    if display.get("acknowledged") is not True:
                        raise RuntimeError(
                            f"sunrise display step was not acknowledged: {display}"
                        )
                    last_display_brightness = display_brightness

            if alarm.audio_source is not None and elapsed >= audio_start:
                if not audio_started:
                    muted = await service.set_volume(0)
                    if muted.get("applied") is not True:
                        raise RuntimeError(f"pre-audio mute was not verified: {muted}")
                    _remember_result(expected_controls, "volume", muted, "volume")
                    # Select the audio source only when audio is due, after muting.
                    # set_input is a no-op when Bluetooth is already selected, which
                    # avoids the redundant source write correlated with the morning
                    # reconnect prompt.
                    selected = await service.set_input("bluetooth")
                    if selected.get("applied") is not True:
                        raise RuntimeError(f"Bluetooth input was not verified: {selected}")
                    _remember_result(expected_controls, "input", selected, "input")
                    player = await _start_audio(alarm)
                    audio_started = True
                    last_volume = 0
                    # A delayed playback notification is the expected consequence of
                    # starting mpv, not a human pressing the side button.
                    expected_controls["playback"] = {"value": 0x0D, "playing": True}
                    playback_events_ignored_until = (
                        asyncio.get_running_loop().time() + 3
                    )
                fade_seconds = alarm.audio_fade_minutes * 60
                audio_progress = (
                    1.0 if fade_seconds == 0 else (elapsed - audio_start) / fade_seconds
                )
                volume = ramp_value(0, alarm.target_speaker_volume, audio_progress)
                if volume != last_volume:
                    result = await service.set_volume(volume)
                    if result.get("applied") is not True:
                        raise RuntimeError(f"sunrise audio step was not verified: {result}")
                    _remember_result(expected_controls, "volume", result, "volume")
                    last_volume = volume

            if elapsed >= duration:
                break
            # Ignore packets already observed during our own command transactions.
            # During the idle interval, snooze only on a parsed state change away
            # from our expected state; this speaker also emits harmless duplicates.
            event_cursor = await _latest_event_sequence(service)
            event_cursor, manual_event = await _wait_for_manual_control(
                service,
                event_cursor,
                min(tick_seconds, max(0.1, duration - elapsed)),
                expected_controls,
                ignore_playback_until=playback_events_ignored_until,
            )
            if manual_event is not None:
                break

        if manual_event is not None:
            player_exit = await _stop_player(player)
            night = await _apply_night_state(service, alarm)
            return {
                "alarm_id": alarm.alarm_id,
                "completed": False,
                "snoozed_by_physical_control": True,
                "snooze_minutes": alarm.physical_snooze_minutes,
                "control_event": manual_event,
                "night_state": night,
                "player_exit_code": player_exit,
                "bluetooth_disconnected": False,
            }

        result: dict[str, object] = {
            "alarm_id": alarm.alarm_id,
            "completed": True,
            "brightness": last_brightness,
            "display_brightness": last_display_brightness,
            "speaker_volume": last_volume,
            "audio_started": audio_started,
            "bluetooth_disconnected": False,
        }
        if player is not None:
            result["player_pid"] = player.pid
            if alarm.post_wake_minutes:
                try:
                    await asyncio.wait_for(
                        player.wait(), alarm.post_wake_minutes * 60
                    )
                except TimeoutError:
                    pass
            result["player_exit_code"] = await _stop_player(player)
            release = await _release_classic_audio(alarm.classic_device)
            result["post_wake_minutes"] = alarm.post_wake_minutes
            result["classic_release"] = release
            result["bluetooth_disconnected"] = release["disconnected"]
        return result
    finally:
        await _stop_player(player)
        await service.disconnect()
        if previous_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_timezone
        if hasattr(time, "tzset"):
            time.tzset()
        _restore_environment(previous_environment)


async def prepare_overnight(alarm: SunriseAlarm) -> dict[str, object]:
    """Apply the dark state and establish Classic audio well before wake time."""
    previous_timezone = os.environ.get("TZ")
    previous_environment = {
        name: os.environ.get(name) for name in _ALARM_ENVIRONMENT_KEYS
    }
    os.environ["TZ"] = alarm.timezone
    if hasattr(time, "tzset"):
        time.tzset()
    os.environ["QD35_DEVICE"] = alarm.ble_device
    os.environ["QD35_ENABLE_WRITES"] = "1"
    os.environ["QD35_SCAN_TIMEOUT"] = "30"
    if alarm.custom_display:
        os.environ["QD35_CUSTOM_DISPLAY_FIRMWARE"] = "1"
    service = create_service()
    keepalive: dict[str, object] | None = None
    try:
        # Firmware 1.3.0 exposes the A400 control advertisement reliably only
        # after the Classic endpoint is connected. Establish that relationship
        # at bedtime, when an unavoidable device prompt is not a wake-up event.
        if alarm.audio_source is not None:
            keepalive = await _set_keepalive_enabled(True)
            if keepalive.get("applied") is not True:
                raise RuntimeError(f"Classic audio keepalive could not start: {keepalive}")
            if not await _wait_for_classic_connection(alarm.classic_device):
                raise RuntimeError("Classic audio did not connect during bedtime preparation")
        await service.set_timezone(alarm.timezone, sync_now=False)
        clock = await service.sync_clock(alarm.clock_format, alarm.timezone)
        night = await _apply_night_state(service, alarm)
        display_value = night["display"]
        display = display_value if isinstance(display_value, dict) else None
        return {
            "alarm_id": alarm.alarm_id,
            "prepared": True,
            "brightness": 0,
            "light_rgb": alarm.light_rgb,
            "display_brightness": alarm.night_display_brightness,
            "clock_visible": False if alarm.custom_display else None,
            "logo_visible": False if alarm.custom_display else None,
            "symbols_visible": False if alarm.custom_display else None,
            "display_acknowledged": (
                display is None or display.get("acknowledged") is True
            ),
            "clock_acknowledged": clock.get("acknowledged") is True,
            "classic_audio_touched": keepalive is not None,
            "classic_keepalive": keepalive,
            "bluetooth_disconnect_command_sent": False,
        }
    finally:
        await service.disconnect()
        if previous_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_timezone
        if hasattr(time, "tzset"):
            time.tzset()
        _restore_environment(previous_environment)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a persisted QD35 sunrise alarm")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--alarm", required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--snoozed", action="store_true")
    return parser.parse_args()


def main() -> None:
    arguments = _parse_args()
    alarms = AlarmStore(arguments.state).load()
    try:
        alarm = alarms[arguments.alarm]
    except KeyError as exc:
        raise SystemExit(f"alarm {arguments.alarm!r} does not exist") from exc
    result: dict[str, object]
    if arguments.prepare:
        result = asyncio.run(prepare_overnight(alarm))
    elif arguments.snoozed:
        fade = alarm.snooze_fade_minutes
        result = asyncio.run(
            run_alarm(
                replace(
                    alarm,
                    sunrise_minutes=fade,
                    audio_fade_minutes=fade,
                    start_brightness=0,
                )
            )
        )
    else:
        result = asyncio.run(run_alarm(alarm))
    if result.get("snoozed_by_physical_control") is True:
        timers = SystemdUserTimers(_alarm_unit_dir(), arguments.state)
        timers.schedule_snooze(alarm.alarm_id, alarm.physical_snooze_minutes)
    print(json.dumps(result, sort_keys=True), file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
