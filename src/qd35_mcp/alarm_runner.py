"""Run one persisted QD35 sunrise alarm under a systemd user service."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import constants as c
from .alarm_schedule import (
    SunriseAlarm,
    active_wake_window,
    in_bedtime_window,
    next_wake_time,
    perceptual_light_value,
    ramp_value,
)
from .alarm_systemd import KEEPALIVE_UNIT, SystemdUserTimers, default_unit_dir
from .alarms import AlarmStore
from .audio_keepalive import disconnect_classic
from .display import DISPLAY_MAGIC, DISPLAY_RESET, DISPLAY_SET_STATE, DISPLAY_WRITE_GRID
from .errors import QD35Error, SpeakerBusy
from .factory import create_service
from .ownership import AudioLease, SpeakerOwnership

_ALARM_ENVIRONMENT_KEYS = (
    "QD35_DEVICE",
    "QD35_ENABLE_WRITES",
    "QD35_CUSTOM_DISPLAY_FIRMWARE",
    "QD35_SCAN_TIMEOUT",
)
_CONTROL_FIELDS = {
    c.QUERY_INPUT: "input",
    c.QUERY_VOLUME: "volume",
    c.QUERY_EQ_PRESET: "eq_preset",
    c.QUERY_LIGHT: "lighting",
    c.SET_LIGHT: "lighting",
    c.RESET_LIGHT: "lighting",
}
_AUDIO_READY = b"QD35_ALARM_AUDIO_READY "


def _alarm_unit_dir() -> Path:
    configured = (os.getenv("QD35_ALARM_UNIT_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return default_unit_dir()


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
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return 124, "command timed out"
    except asyncio.CancelledError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
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
    prefix = f"bluez_output.{address.upper().replace(':', '_')}"
    for _attempt in range(20):
        code, output = await _run_command("pactl", "list", "short", "sinks")
        if code == 0:
            for line in output.splitlines():
                fields = line.split("\t")
                if len(fields) >= 2 and (fields[1] == prefix or fields[1].startswith(f"{prefix}.")):
                    return fields[1]
        await asyncio.sleep(1)
    raise RuntimeError("QD35 connected but no PipeWire/PulseAudio sink appeared")


async def _confirm_audio_route(sink: str, stream_name: str) -> None:
    for attempt in range(8):
        sink_code, sinks = await _run_command("pactl", "-f", "json", "list", "sinks", timeout=2)
        stream_code, streams = await _run_command(
            "pactl", "-f", "json", "list", "sink-inputs", timeout=2
        )
        if sink_code == stream_code == 0:
            target = next(
                (item["index"] for item in json.loads(sinks) if item["name"] == sink), None
            )
            for stream in json.loads(streams):
                if stream.get("properties", {}).get("node.name") == stream_name:
                    if target is not None and stream.get("sink") == target:
                        return
                    break
        if attempt < 7:
            await asyncio.sleep(0.25)
    raise RuntimeError(f"mpv did not link its audio stream to {sink}")


async def _start_audio(alarm: SunriseAlarm) -> asyncio.subprocess.Process:
    if alarm.audio_source is None:
        raise ValueError("alarm has no audio source")
    sink = await _find_audio_sink(alarm.classic_device)
    stream_name = f"qd35-alarm-{uuid4().hex}"
    env = {**os.environ}
    # WirePlumber otherwise falls back to a local sink if Bluetooth disappears.
    env["PIPEWIRE_PROPS"] = "{ node.dont-fallback = true node.dont-reconnect = true }"
    process = await asyncio.create_subprocess_exec(
        "mpv",
        "--no-config",
        "--no-video",
        "--no-input-terminal",
        "--msg-level=all=info",
        "--term-status-msg=",
        "--term-playing-msg=QD35_ALARM_AUDIO_READY ${audio-out-params/samplerate}",
        f"--audio-device=pipewire/{sink}",
        f"--audio-client-name={stream_name}",
        "--volume=50",
        "--loop-playlist=inf",
        "--network-timeout=15",
        "--",
        alarm.audio_source,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    ready: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    output = asyncio.create_task(_watch_audio_output(process, ready))
    try:
        if not await asyncio.wait_for(ready, 20) or process.returncode is not None:
            raise RuntimeError(f"mpv exited before alarm playback (exit {process.returncode})")
        await _confirm_audio_route(sink, stream_name)
        if process.returncode is not None:
            raise RuntimeError(f"mpv exited before the volume fade (exit {process.returncode})")
    except (Exception, asyncio.CancelledError):
        await _stop_player(process)
        output.cancel()
        await asyncio.gather(output, return_exceptions=True)
        raise
    return process


async def _watch_audio_output(
    process: asyncio.subprocess.Process, ready: asyncio.Future[bool]
) -> None:
    assert process.stdout is not None
    try:
        async for line in process.stdout:
            candidate = line.strip()
            if candidate.startswith(_AUDIO_READY) and not ready.done():
                rate = candidate.removeprefix(_AUDIO_READY)
                if rate.isdigit() and int(rate):
                    ready.set_result(True)
            if candidate.startswith(b"[ao/pipewire] Error during playback:"):
                with suppress(ProcessLookupError):
                    process.terminate()
    finally:
        if not ready.done():
            ready.set_result(False)


async def _stop_player(player: asyncio.subprocess.Process | None) -> int | None:
    if player is None:
        return None
    if player.returncode is None:
        with suppress(ProcessLookupError):
            player.terminate()
        try:
            await asyncio.wait_for(player.wait(), 5)
        except TimeoutError:
            with suppress(ProcessLookupError):
                player.kill()
            await player.wait()
    return player.returncode


async def _set_keepalive_enabled(enabled: bool) -> dict[str, object]:
    action = ("start",) if enabled else ("disable", "--now")
    code, output = await _run_command("systemctl", "--user", *action, KEEPALIVE_UNIT)
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
    if not disconnected:
        info_code, info = await _run_command("bluetoothctl", "info", address)
        disconnected = info_code == 0 and "Connected: no" in info
    return {
        "keepalive": keepalive,
        "disconnect_exit": code,
        "disconnect_detail": output,
        "disconnected": disconnected,
    }


async def _release_expired_keepalive(alarm: SunriseAlarm) -> dict[str, object] | None:
    ownership = SpeakerOwnership()
    lease = ownership.audio_state(alarm.classic_device).lease
    if lease is not None and lease.alarm_id == alarm.alarm_id:
        outcome = await ownership.release_audio(
            alarm.classic_device, lease, disconnect_classic, expired_only=True
        )
        return {"outcome": outcome, "disconnected": outcome == "disconnected"}
    return None


async def _latest_event_sequence(service: Any) -> int:
    result = await service.events(limit=1)
    return int(result.get("latest_sequence", 0))


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
    return False


async def _wait_for_manual_control(
    service: Any,
    since_sequence: int,
    timeout: float,
    expected: dict[str, object],
) -> tuple[int, dict[str, object] | None]:
    """Treat notifications as hints; a fresh read must confirm a control change."""
    deadline = asyncio.get_running_loop().time() + timeout
    cursor = since_sequence
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return cursor, None
        await asyncio.sleep(min(0.25, remaining))
        result = await service.events(since_sequence=cursor, limit=100)
        for raw_event in result.get("events", []):
            event = dict(raw_event)
            opcode = event.get("opcode")
            cursor = int(event.get("sequence", cursor))
            if opcode not in _CONTROL_FIELDS or not _manual_control_changed(event, expected):
                continue
            field = _CONTROL_FIELDS[opcode]
            try:
                current = await service.alarm_control(field)
            except QD35Error:
                continue
            query_opcode = c.QUERY_LIGHT if field == "lighting" else opcode
            if _manual_control_changed({"opcode": query_opcode, "parsed": current}, expected):
                return cursor, event
        if not result.get("more"):
            cursor = int(result.get("latest_sequence", cursor))


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


async def _arm_audio(
    service: Any,
    alarm: SunriseAlarm,
    expected: dict[str, object],
    ownership: SpeakerOwnership | None,
    lease: AudioLease | None,
) -> asyncio.subprocess.Process:
    if ownership is not None and lease is not None:
        async with ownership.lock("audio", alarm.classic_device):
            current = ownership.audio_state(alarm.classic_device).lease
            remaining = lease.expires_at - time.time()
            if current is None or current.token != lease.token or remaining <= 0:
                raise SpeakerBusy("ALARM_ACTIVE: alarm no longer owns QD35 audio")
            await asyncio.wait_for(_connect_classic(alarm.classic_device), remaining)
    else:
        await _connect_classic(alarm.classic_device)
    muted = await service.set_volume(0)
    if muted.get("applied") is not True:
        raise RuntimeError("pre-audio mute was not verified")
    expected["volume"] = {"current": 0}
    selected = await service.set_input("bluetooth")
    if selected.get("applied") is not True:
        raise RuntimeError("Bluetooth input was not verified")
    expected["input"] = {"group": 2, "value": c.INPUTS["bluetooth"]}
    return await _start_audio(alarm)


async def run_alarm(
    alarm: SunriseAlarm,
    *,
    tick_seconds: float = 30,
    scheduled_window: tuple[datetime, datetime] | None = None,
    ownership: SpeakerOwnership | None = None,
) -> dict[str, object]:
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
    audio_attempts = 0
    lease: AudioLease | None = None
    release_audio = alarm.audio_source is not None
    result: dict[str, object] = {}
    errors: dict[str, str] = {}
    last_brightness: int | None = None
    last_display_brightness: int | None = None
    last_volume: int | None = None
    if scheduled_window is None:
        clock = time.monotonic
        started = clock()
        duration = alarm.sunrise_minutes * 60
    else:
        start_at, wake_at = scheduled_window
        clock = time.time
        started = start_at.timestamp()
        duration = max(0.0, (wake_at - start_at).total_seconds())
    audio_start = max(0, duration - alarm.audio_fade_minutes * 60)
    wake_end = started + duration + alarm.post_wake_minutes * 60
    try:
        if ownership is not None and alarm.audio_source is not None:
            now = time.time()
            expires_at = (
                wake_end if scheduled_window is not None
                else now + duration + alarm.post_wake_minutes * 60 + 30
            )
            reconnect_at = (
                started + audio_start if scheduled_window is not None else now + audio_start
            )
            keepalive = await _set_keepalive_enabled(True)
            if keepalive["applied"] is True:
                lease = await ownership.claim_audio(
                    alarm.classic_device, alarm.alarm_id, expires_at, reconnect_at
                )
            else:
                errors["audio"] = f"Could not start the QD35 audio lease worker: {keepalive}"
        try:
            await service.sync_clock(alarm.clock_format, alarm.timezone)
        except QD35Error as exc:
            errors["clock"] = str(exc)
        expected_controls: dict[str, object] = {}
        # Skip optional baselines on an unavailable control link so audio can try
        # to recover it later. One failed read is enough to stop the initial sweep.
        for field in ("volume", "input", "eq_preset") if "clock" not in errors else ():
            try:
                expected_controls[field] = await service.alarm_control(field)
            except QD35Error:
                break
        event_cursor = await _latest_event_sequence(service)
        manual_event: dict[str, object] | None = None
        while True:
            elapsed = min(duration, max(0, clock() - started))
            light_progress = 1.0 if duration == 0 else elapsed / duration
            brightness = perceptual_light_value(
                alarm.start_brightness, alarm.end_brightness, light_progress
            )
            if brightness != last_brightness:
                try:
                    light = await service.set_light(
                        effect="static", rgb=alarm.light_rgb, brightness=brightness
                    )
                    if light.get("applied") is not True:
                        raise RuntimeError(f"sunrise light step was not verified: {light}")
                except (QD35Error, RuntimeError) as exc:
                    errors["lighting"] = str(exc)
                else:
                    errors.pop("lighting", None)
                    _remember_result(expected_controls, "lighting", light, "lighting")
                    last_brightness = brightness
            if alarm.custom_display:
                display_brightness = perceptual_light_value(0, 7, light_progress)
                if display_brightness != last_display_brightness:
                    try:
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
                    except (QD35Error, RuntimeError) as exc:
                        errors["display"] = str(exc)
                    else:
                        errors.pop("display", None)
                        last_display_brightness = display_brightness

            now = clock()
            elapsed = min(duration, max(0, now - started))
            audio_allowed = ownership is None or lease is not None
            if alarm.audio_source is not None and audio_allowed and elapsed >= audio_start:
                if player is not None and player.returncode is not None:
                    errors["audio"] = f"mpv exited during sunrise (exit {player.returncode})"
                    player = None
                attempts_this_step = 3 - audio_attempts if elapsed >= duration else 1
                enforce_cutoff = scheduled_window is not None and alarm.post_wake_minutes > 0
                for _ in range(attempts_this_step):
                    if player is not None or audio_attempts >= 3:
                        break
                    if enforce_cutoff and clock() >= wake_end:
                        break
                    audio_attempts += 1
                    try:
                        player = await _arm_audio(
                            service, alarm, expected_controls, ownership, lease
                        )
                    except (QD35Error, OSError, RuntimeError) as exc:
                        errors["audio"] = str(exc) or type(exc).__name__
                    else:
                        audio_started = True
                        last_volume = 0
                        errors.pop("audio", None)
                if enforce_cutoff and clock() >= wake_end:
                    if last_volume == 0 and alarm.target_speaker_volume > 0:
                        errors.setdefault("audio", "the audio window expired before volume rose")
                elif player is not None and player.returncode is None:
                    fade_seconds = alarm.audio_fade_minutes * 60
                    audio_progress = (
                        1.0 if not fade_seconds else (elapsed - audio_start) / fade_seconds
                    )
                    volume = ramp_value(0, alarm.target_speaker_volume, audio_progress)
                    if volume != last_volume:
                        try:
                            volume_result = await service.set_volume(volume)
                            if volume_result.get("applied") is not True:
                                raise RuntimeError(f"sunrise volume step failed: {volume_result}")
                        except (QD35Error, RuntimeError) as exc:
                            errors["volume"] = str(exc)
                        else:
                            errors.pop("volume", None)
                            _remember_result(expected_controls, "volume", volume_result, "volume")
                            last_volume = volume

            if light_progress >= 1:
                break
            next_step = min(tick_seconds, max(0.1, duration - elapsed))
            if alarm.audio_source is not None and elapsed < audio_start:
                next_step = min(next_step, audio_start - elapsed)
            event_cursor, manual_event = await _wait_for_manual_control(
                service,
                event_cursor,
                next_step,
                expected_controls,
            )
            if manual_event is not None:
                break

        if manual_event is not None:
            player_exit = await _stop_player(player)
            try:
                night = await _apply_night_state(service, alarm)
            except (QD35Error, RuntimeError) as exc:
                night = {"error": str(exc)}
            if ownership is not None and lease is not None:
                snooze_seconds = 60 * (
                    alarm.physical_snooze_minutes
                    + alarm.snooze_fade_minutes
                    + alarm.post_wake_minutes
                )
                await ownership.extend_alarm(
                    alarm.classic_device, alarm.alarm_id, time.time() + snooze_seconds + 30
                )
            release_audio = False
            result = {
                "alarm_id": alarm.alarm_id,
                "completed": False,
                "snoozed_by_physical_control": True,
                "snooze_minutes": alarm.physical_snooze_minutes,
                "control_event": manual_event,
                "night_state": night,
                "player_exit_code": player_exit,
                "bluetooth_disconnected": False,
            }
            return result

        result = {
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
            # Timer-triggered runs keep the original end time after sleep or a late start.
            if scheduled_window is None:
                wake_end = clock() + alarm.post_wake_minutes * 60
            remaining = wake_end - clock()
            while remaining > 0:
                try:
                    await asyncio.wait_for(player.wait(), min(remaining, 30))
                except TimeoutError:
                    remaining = wake_end - clock()
                else:
                    errors["audio"] = (
                        f"mpv exited before the end of the wake window (exit {player.returncode})"
                    )
                    break
            result["player_exit_code"] = await _stop_player(player)
            result["post_wake_minutes"] = alarm.post_wake_minutes
        elif alarm.audio_source is not None and "audio" not in errors:
            errors["audio"] = "the scheduled audio window expired before playback started"
        if errors:
            result["errors"] = errors
            result["completed"] = not (errors.keys() - {"clock"})
        return result
    except asyncio.CancelledError:
        release_audio = False
        raise
    finally:
        try:
            try:
                await _stop_player(player)
                await service.disconnect()
            finally:
                if release_audio and (ownership is None or lease is not None):
                    if ownership is None:
                        release = await _release_classic_audio(alarm.classic_device)
                        failed = not release["disconnected"]
                    else:
                        outcome = await ownership.release_audio(
                            alarm.classic_device, lease, disconnect_classic
                        )
                        release = {"outcome": outcome, "disconnected": outcome == "disconnected"}
                        failed = outcome == "disconnect_failed"
                    result["classic_release"] = release
                    result["bluetooth_disconnected"] = release["disconnected"]
                    if failed:
                        result.setdefault("errors", {})["classic_release"] = release
                        result["completed"] = False
        finally:
            _restore_environment({"TZ": previous_timezone, **previous_environment})
            if hasattr(time, "tzset"):
                time.tzset()


async def prepare_overnight(
    alarm: SunriseAlarm, *, ownership: SpeakerOwnership | None = None
) -> dict[str, object]:
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
            if ownership is not None:
                now = datetime.now(UTC)
                expires_at = next_wake_time(alarm, now).timestamp() + alarm.post_wake_minutes * 60
                await ownership.claim_audio(
                    alarm.classic_device, alarm.alarm_id, expires_at, now.timestamp()
                )
            if not await _wait_for_classic_connection(alarm.classic_device):
                raise RuntimeError("Classic audio did not connect during bedtime preparation")
        # A clock sync failure should not prevent the night lighting from being applied.
        try:
            clock = await service.sync_clock(alarm.clock_format, alarm.timezone)
        except QD35Error as exc:
            clock = {"acknowledged": False, "error": str(exc)}
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
            **({"clock_error": clock["error"]} if "error" in clock else {}),
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


async def _run_owned(
    alarm: SunriseAlarm,
    *,
    prepare: bool = False,
    scheduled_window: tuple[datetime, datetime] | None = None,
) -> dict[str, object]:
    ownership = SpeakerOwnership()
    try:
        async with ownership.lock("alarm", alarm.ble_device, timeout=0):
            if prepare:
                return await prepare_overnight(alarm, ownership=ownership)
            return await run_alarm(alarm, scheduled_window=scheduled_window, ownership=ownership)
    except SpeakerBusy as exc:
        return {"alarm_id": alarm.alarm_id, "completed": False, "error": str(exc)}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a persisted QD35 sunrise alarm")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--alarm", required=True)
    parser.add_argument("--unit-dir", type=Path)
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
    now = datetime.now(UTC)
    triggered_by = os.getenv("TRIGGER_UNIT")
    result: dict[str, object]
    if arguments.prepare:
        prepare_timer = f"qd35-alarm-{alarm.alarm_id}-prepare.timer"
        if triggered_by == prepare_timer and not in_bedtime_window(alarm, now):
            result = {"alarm_id": alarm.alarm_id, "skipped": "bedtime window has passed"}
        else:
            result = asyncio.run(_run_owned(alarm, prepare=True))
    elif arguments.snoozed:
        fade = alarm.snooze_fade_minutes
        result = asyncio.run(
            _run_owned(
                replace(
                    alarm,
                    sunrise_minutes=fade,
                    audio_fade_minutes=fade,
                    start_brightness=0,
                )
            )
        )
    elif triggered_by == f"qd35-alarm-{alarm.alarm_id}.timer":
        window = active_wake_window(alarm, now)
        if window is None:
            result = {"alarm_id": alarm.alarm_id, "skipped": "wake window has passed"}
            if alarm.audio_source is not None and not in_bedtime_window(alarm, now):
                result["classic_release"] = asyncio.run(_release_expired_keepalive(alarm))
        else:
            result = asyncio.run(_run_owned(alarm, scheduled_window=window))
    else:
        result = asyncio.run(_run_owned(alarm))
    print(json.dumps(result, sort_keys=True), file=sys.stderr, flush=True)
    if result.get("snoozed_by_physical_control") is True:
        timers = SystemdUserTimers(arguments.unit_dir or _alarm_unit_dir(), arguments.state)
        timers.schedule_snooze(alarm.alarm_id, alarm.physical_snooze_minutes)
    elif result.get("completed") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
