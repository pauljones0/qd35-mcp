"""Run one persisted QD35 sunrise alarm under a systemd user service."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from dataclasses import replace
from pathlib import Path

from .alarms import AlarmStore, SunriseAlarm, perceptual_light_value, ramp_value
from .server import create_service

_ALARM_ENVIRONMENT_KEYS = (
    "QD35_DEVICE",
    "QD35_ENABLE_WRITES",
    "QD35_CUSTOM_DISPLAY_FIRMWARE",
    "QD35_SCAN_TIMEOUT",
)


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
    environment = {**os.environ, "PULSE_SINK": sink}
    return await asyncio.create_subprocess_exec(
        "mpv",
        "--no-video",
        "--really-quiet",
        "--volume=50",
        "--loop-playlist=inf",
        "--",
        alarm.audio_source,
        env=environment,
    )


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
    try:
        await service.set_timezone(alarm.timezone, sync_now=False)
        await service.sync_clock(alarm.clock_format, alarm.timezone)
        await service.set_input("bluetooth")
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
                    await service.set_volume(0)
                    player = await _start_audio(alarm)
                    audio_started = True
                fade_seconds = alarm.audio_fade_minutes * 60
                audio_progress = (
                    1.0 if fade_seconds == 0 else (elapsed - audio_start) / fade_seconds
                )
                volume = ramp_value(0, alarm.target_speaker_volume, audio_progress)
                if volume != last_volume:
                    result = await service.set_volume(volume)
                    if result.get("applied") is not True:
                        raise RuntimeError(f"sunrise audio step was not verified: {result}")
                    last_volume = volume

            if elapsed >= duration:
                break
            await asyncio.sleep(min(tick_seconds, max(0.1, duration - elapsed)))

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
            await player.wait()
            result["player_exit_code"] = player.returncode
        return result
    finally:
        await service.disconnect()
        if previous_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_timezone
        if hasattr(time, "tzset"):
            time.tzset()
        _restore_environment(previous_environment)


async def prepare_overnight(alarm: SunriseAlarm) -> dict[str, object]:
    """Apply the low overnight light state without touching Classic audio."""
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
    try:
        await service.set_timezone(alarm.timezone, sync_now=False)
        clock = await service.sync_clock(alarm.clock_format, alarm.timezone)
        light = await service.set_light(
            effect="static",
            rgb=alarm.light_rgb,
            brightness=0,
        )
        if light.get("applied") is not True:
            raise RuntimeError(f"overnight light state was not verified: {light}")
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
            "classic_audio_touched": False,
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
    if arguments.prepare:
        asyncio.run(prepare_overnight(alarm))
    elif arguments.snoozed:
        fade = alarm.snooze_fade_minutes
        asyncio.run(
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
        asyncio.run(run_alarm(alarm))


if __name__ == "__main__":
    main()
