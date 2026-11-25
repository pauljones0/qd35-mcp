"""Run one persisted QD35 sunrise alarm under a systemd user service."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import constants as c
from .alarm_schedule import (
    AUDIO_CLEANUP_GRACE_SECONDS,
    SunriseAlarm,
    active_wake_window,
    in_bedtime_window,
    next_wake_time,
    perceptual_light_value,
    ramp_value,
    start_for_wake,
    wake_cutoff,
)
from .alarm_systemd import KEEPALIVE_UNIT, SystemdUserTimers, default_unit_dir
from .alarms import AlarmStore, apply_night_state, restore_daytime_state
from .audio_keepalive import disconnect_classic
from .config import Config
from .display import (
    DISPLAY_MAGIC,
    DISPLAY_RESET,
    DISPLAY_SET_STATE,
    DISPLAY_UPDATE_STATE,
    DISPLAY_WRITE_GRID,
)
from .errors import QD35Error, SpeakerBusy
from .factory import create_service
from .ownership import AudioLease, SpeakerOwnership

_ALARM_ENVIRONMENT_KEYS = (
    "QD35_DEVICE",
    "QD35_ENABLE_WRITES",
    "QD35_CUSTOM_DISPLAY_FIRMWARE",
    "QD35_SCAN_TIMEOUT",
)
_ALARM_PLAYER_GAIN = 50
_AUDIO_ATTEMPTS = 3
_FADE_OUT_STEP_SECONDS = 2.0
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
        code, output = await _run_command("pactl", "list", "short", "sinks", timeout=2)
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
            try:
                target = next(
                    (item["index"] for item in json.loads(sinks) if item["name"] == sink), None
                )
                for stream in json.loads(streams):
                    if stream.get("properties", {}).get("node.name") == stream_name:
                        if target is not None and stream.get("sink") == target:
                            return
                        break
            except (ValueError, TypeError, KeyError, AttributeError):
                # Retry a snapshot taken while PipeWire recreates the sink.
                pass
        if attempt < 7:
            await asyncio.sleep(0.25)
    raise RuntimeError(f"mpv did not link its audio stream to {sink}")


@dataclass(slots=True)
class _AlarmPlayer:
    process: asyncio.subprocess.Process
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    request_id: int = 0

    async def set_gain(self, gain: int) -> None:
        self.request_id += 1
        request = {"command": ["set_property", "volume", gain], "request_id": self.request_id}
        async with asyncio.timeout(2):
            self.writer.write(json.dumps(request).encode() + b"\n")
            await self.writer.drain()
            while reply := await self.reader.readline():
                message = json.loads(reply)
                if message.get("request_id") == self.request_id:
                    if message.get("error") == "success":
                        return
                    raise RuntimeError(f"mpv rejected software gain: {message.get('error')}")
        raise RuntimeError("mpv closed its control connection during the audio fade")


async def _start_audio(alarm: SunriseAlarm) -> _AlarmPlayer:
    if alarm.audio_source is None:
        raise ValueError("alarm has no audio source")
    sink = await _find_audio_sink(alarm.classic_device)
    stream_name = f"qd35-alarm-{uuid4().hex}"
    env = {**os.environ}
    # WirePlumber otherwise falls back to a local sink if Bluetooth disappears.
    env["PIPEWIRE_PROPS"] = "{ node.dont-fallback = true node.dont-reconnect = true }"
    control, inherited = socket.socketpair()
    with inherited:
        try:
            reader, writer = await asyncio.open_connection(sock=control)
        except (Exception, asyncio.CancelledError):
            control.close()
            raise
        try:
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
                f"--input-ipc-client=fd://{inherited.fileno()}",
                "--volume=0",
                "--loop-playlist=inf",
                "--network-timeout=15",
                "--",
                alarm.audio_source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                pass_fds=(inherited.fileno(),),
                env=env,
            )
        except (Exception, asyncio.CancelledError):
            writer.close()
            raise
    player = _AlarmPlayer(process, reader, writer)
    ready: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    output = asyncio.create_task(_watch_audio_output(process, ready))
    try:
        if not await asyncio.wait_for(ready, 20) or process.returncode is not None:
            raise RuntimeError(f"mpv exited before alarm playback (exit {process.returncode})")
        await _confirm_audio_route(sink, stream_name)
        if process.returncode is not None:
            raise RuntimeError(f"mpv exited before the volume fade (exit {process.returncode})")
    except (Exception, asyncio.CancelledError):
        await _stop_player(player)
        output.cancel()
        await asyncio.gather(output, return_exceptions=True)
        raise
    return player


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


async def _stop_player(player: _AlarmPlayer | None) -> int | None:
    if player is None:
        return None
    player.writer.close()
    process = player.process
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    return process.returncode


async def _enforce_player_cutoff(
    player: _AlarmPlayer, clock: Callable[[], float], deadline: float,
) -> None:
    # Playback must stop even while another task is stuck in a Bluetooth transaction.
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        await asyncio.sleep(min(remaining, 1))
    await _stop_player(player)


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


async def _finish_missed_snooze(alarm: SunriseAlarm) -> dict[str, object]:
    ownership = SpeakerOwnership()
    try:
        async with ownership.lock("alarm", alarm.ble_device, timeout=0):
            service = create_service(replace(
                Config.from_env(), device=alarm.ble_device, writes_enabled=True,
                custom_display_firmware=alarm.custom_display,
            ))
            try:
                result = {"daytime_state": await restore_daytime_state(service, alarm)}
            finally:
                await service.disconnect()
            result["classic_release"] = await _release_expired_keepalive(alarm)
            return result
    except SpeakerBusy as exc:
        return {"daytime_state_skipped": str(exc)}


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
        and payload[2] in {
            DISPLAY_SET_STATE, DISPLAY_WRITE_GRID, DISPLAY_RESET, DISPLAY_UPDATE_STATE
        }
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
    tried_reconnect = False
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return cursor, None
        if not service.alarm_control_connected and not tried_reconnect:
            tried_reconnect = True
            # Leave the subscription initialized, including the device's D8 handshake.
            # The caller's cutoff can still cancel this independently of audio.
            try:
                current = await service.reconnect_alarm_controls()
                expected.setdefault("volume", current)
            except QD35Error:
                pass
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


class _PlaybackEnded(RuntimeError):
    def __init__(self, code: int, cursor: int) -> None:
        super().__init__(f"mpv exited before the end of the wake window (exit {code})")
        self.cursor = cursor


async def _wait_after_fade(
    service: Any,
    player: _AlarmPlayer,
    cursor: int,
    expected: dict[str, object],
    clock: Callable[[], float],
    wake_end: float,
    *,
    fade_out_seconds: float = 0,
    fade_step_seconds: float = _FADE_OUT_STEP_SECONDS,
    on_fade_step: Callable[[float], Awaitable[None]] | None = None,
) -> dict[str, object] | None:
    """Keep accepting physical snooze while the radio plays at its target level."""
    playback = asyncio.create_task(player.process.wait())
    fade_start = (
        wake_end - fade_out_seconds
        if fade_out_seconds > 0 and on_fade_step is not None
        else wake_end
    )
    try:
        while True:
            now = clock()
            remaining = wake_end - now
            if remaining <= 0:
                break
            if on_fade_step is not None and fade_out_seconds > 0 and now >= fade_start:
                await on_fade_step(now)
                if playback.done() and clock() < wake_end:
                    raise _PlaybackEnded(playback.result(), cursor)
                after_step = clock()
                remaining = wake_end - after_step
                if remaining <= 0:
                    break
                step_remaining = (now + fade_step_seconds) - after_step
                interval = min(
                    remaining,
                    max(min(0.05, fade_step_seconds), step_remaining),
                )
            else:
                interval = min(remaining, fade_start - now, 60)
            wait_timeout = (
                min(remaining, interval + 0.25) if interval < remaining else remaining
            )
            controls = asyncio.create_task(
                _wait_for_manual_control(service, cursor, interval, expected)
            )
            try:
                done, _ = await asyncio.wait(
                    {playback, controls},
                    timeout=wait_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if controls in done:
                    cursor, event = controls.result()
                    if event is not None:
                        return event
                if playback in done and clock() < wake_end:
                    raise _PlaybackEnded(playback.result(), cursor)
            finally:
                controls.cancel()
                await asyncio.gather(controls, return_exceptions=True)
        return None
    finally:
        playback.cancel()
        await asyncio.gather(playback, return_exceptions=True)


async def _arm_audio(
    service: Any,
    alarm: SunriseAlarm,
    expected: dict[str, object],
    ownership: SpeakerOwnership | None,
    lease: AudioLease | None,
) -> _AlarmPlayer:
    if ownership is not None and lease is not None:
        async with ownership.lock("audio", alarm.classic_device):
            current = ownership.audio_state(alarm.classic_device).lease
            remaining = lease.expires_at - time.time()
            if current is None or current.token != lease.token or remaining <= 0:
                raise SpeakerBusy("ALARM_ACTIVE: alarm no longer owns QD35 audio")
            await asyncio.wait_for(_connect_classic(alarm.classic_device), remaining)
    else:
        await _connect_classic(alarm.classic_device)
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
    cutoff: datetime | None = None,
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
    player: _AlarmPlayer | None = None
    audio_started = False
    audio_attempts = 0
    lease: AudioLease | None = None
    release_audio = alarm.audio_source is not None
    result: dict[str, object] = {}
    errors: dict[str, str] = {}
    last_brightness: int | None = None
    last_display_brightness: int | None = None
    last_volume: int | None = None
    last_gain: int | None = None
    cutoff_task: asyncio.Task[None] | None = None
    if scheduled_window is None:
        clock = time.monotonic
        started = clock()
        duration = alarm.sunrise_minutes * 60
    else:
        start_at, wake_at = scheduled_window
        clock = time.time
        started = start_at.timestamp()
        duration = max(0.0, (wake_at - start_at).total_seconds())
        cutoff = cutoff or wake_cutoff(alarm, wake_at)
    audio_start = max(0, duration - alarm.audio_fade_minutes * 60)
    wake_end = cutoff.timestamp() if cutoff else started + duration + alarm.post_wake_minutes * 60
    expected_controls: dict[str, object] = {}

    async def stop_audio() -> int | None:
        nonlocal cutoff_task
        if cutoff_task is not None:
            cutoff_task.cancel()
            await asyncio.gather(cutoff_task, return_exceptions=True)
            cutoff_task = None
        return await _stop_player(player)

    async def start_audio(*, enforce_cutoff: bool) -> None:
        nonlocal player, cutoff_task, audio_started, last_volume, last_gain
        async with asyncio.timeout(max(0, wake_end - clock()) if enforce_cutoff else None):
            player = await _arm_audio(service, alarm, expected_controls, ownership, lease)
        if enforce_cutoff:
            cutoff_task = asyncio.create_task(_enforce_player_cutoff(player, clock, wake_end))
        audio_started = True
        last_volume = None
        last_gain = 0
        errors.pop("audio", None)

    async def set_audio_gain(gain: int, *, enforce_cutoff: bool) -> None:
        nonlocal last_volume, last_gain
        assert player is not None
        # Hardware volume changes replace the RGB frame with the volume bar.
        if last_volume is None:
            target = alarm.target_speaker_volume
            current = await service.alarm_control("volume")
            if enforce_cutoff and clock() >= wake_end:
                return
            if current.get("current") != target:
                volume = await service.set_volume(target)
                if volume.get("applied") is not True:
                    raise RuntimeError(f"alarm speaker volume did not verify: {volume}")
            last_volume = target
            expected_controls["volume"] = {"current": target}
        if gain != last_gain and (not enforce_cutoff or clock() < wake_end):
            await player.set_gain(gain)
            last_gain = gain

    try:
        if ownership is not None and alarm.audio_source is not None:
            now = time.time()
            expires_at = AUDIO_CLEANUP_GRACE_SECONDS + (
                wake_end if scheduled_window is not None
                else now + duration + alarm.post_wake_minutes * 60
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
                enforce_cutoff = cutoff is not None
                if (player is not None and player.process.returncode is not None
                    and (not enforce_cutoff or clock() < wake_end)):
                    code = player.process.returncode
                    errors["audio"] = f"mpv exited during sunrise (exit {code})"
                    await stop_audio()
                    player = None
                attempts_this_step = _AUDIO_ATTEMPTS - audio_attempts if elapsed >= duration else 1
                for _ in range(attempts_this_step):
                    if player is not None or audio_attempts >= _AUDIO_ATTEMPTS:
                        break
                    if enforce_cutoff and clock() >= wake_end:
                        break
                    audio_attempts += 1
                    try:
                        await start_audio(enforce_cutoff=enforce_cutoff)
                    except (QD35Error, OSError, RuntimeError) as exc:
                        errors["audio"] = str(exc) or type(exc).__name__
                if enforce_cutoff and clock() >= wake_end:
                    if not last_gain and alarm.target_speaker_volume > 0:
                        errors.setdefault("audio", "the audio window expired before volume rose")
                elif player is not None and player.process.returncode is None:
                    fade_seconds = alarm.audio_fade_minutes * 60
                    audio_progress = (
                        1.0 if not fade_seconds else (elapsed - audio_start) / fade_seconds
                    )
                    gain = ramp_value(0, _ALARM_PLAYER_GAIN, audio_progress)
                    try:
                        await set_audio_gain(gain, enforce_cutoff=enforce_cutoff)
                    except (QD35Error, OSError, RuntimeError, TimeoutError, ValueError) as exc:
                        if not enforce_cutoff or clock() < wake_end:
                            errors["volume"] = str(exc) or type(exc).__name__
                    else:
                        errors.pop("volume", None)

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

        if manual_event is None and player is not None and alarm.post_wake_minutes > 0:
            # Timer-triggered runs keep the original end time after sleep or a late start.
            if scheduled_window is None:
                wake_end = clock() + alarm.post_wake_minutes * 60
                cutoff_task = asyncio.create_task(_enforce_player_cutoff(player, clock, wake_end))
            post_wake_start = clock()
            fade_out_seconds = min(
                alarm.fade_out_minutes * 60, max(0.0, wake_end - post_wake_start)
            )
            fade_out_start = wake_end - fade_out_seconds
            fade_start_gain = last_gain if last_gain is not None else _ALARM_PLAYER_GAIN
            fade_start_brightness = last_brightness or 0
            fade_step_seconds = min(tick_seconds, _FADE_OUT_STEP_SECONDS)

            def fade_out_fraction(now: float) -> float:
                if fade_out_seconds <= 0 or now <= fade_out_start:
                    return 1.0
                return min(1.0, max(0.0, (wake_end - now) / fade_out_seconds))

            async def apply_fade_out_step(now: float) -> None:
                nonlocal last_brightness
                fraction = fade_out_fraction(now)
                if player is not None and player.process.returncode is None and clock() < wake_end:
                    gain = ramp_value(0, fade_start_gain, fraction)
                    try:
                        await set_audio_gain(gain, enforce_cutoff=True)
                    except (QD35Error, OSError, RuntimeError, TimeoutError, ValueError) as exc:
                        if clock() < wake_end:
                            errors["volume"] = str(exc) or type(exc).__name__
                    else:
                        errors.pop("volume", None)
                if fade_start_brightness > 0 and clock() < wake_end:
                    brightness = perceptual_light_value(0, fade_start_brightness, fraction)
                    if brightness != last_brightness:
                        try:
                            light = await service.set_light(
                                effect="static", rgb=alarm.light_rgb, brightness=brightness
                            )
                            if light.get("applied") is not True:
                                raise RuntimeError(
                                    f"fade-out light step was not verified: {light}"
                                )
                        except (QD35Error, RuntimeError) as exc:
                            errors["lighting"] = str(exc)
                        else:
                            errors.pop("lighting", None)
                            _remember_result(expected_controls, "lighting", light, "lighting")
                            last_brightness = brightness

            # Give steady playback its own small recovery budget. A bad station
            # cannot retry indefinitely or move the original cutoff.
            retries = 2
            while clock() < wake_end:
                if player is not None:
                    try:
                        if fade_out_seconds > 0:
                            manual_event = await _wait_after_fade(
                                service,
                                player,
                                event_cursor,
                                expected_controls,
                                clock,
                                wake_end,
                                fade_out_seconds=fade_out_seconds,
                                fade_step_seconds=fade_step_seconds,
                                on_fade_step=apply_fade_out_step,
                            )
                        else:
                            manual_event = await _wait_after_fade(
                                service,
                                player,
                                event_cursor,
                                expected_controls,
                                clock,
                                wake_end,
                            )
                        break
                    except _PlaybackEnded as exc:
                        event_cursor = exc.cursor
                        errors["audio"] = str(exc)
                        await stop_audio()
                        player = None
                if not retries or clock() >= wake_end:
                    break
                recovery_gain = ramp_value(0, fade_start_gain, fade_out_fraction(clock()))
                if fade_out_seconds > 0 and recovery_gain <= 0:
                    break
                retries -= 1
                try:
                    await start_audio(enforce_cutoff=True)
                    recovery_gain = ramp_value(0, fade_start_gain, fade_out_fraction(clock()))
                    await set_audio_gain(recovery_gain, enforce_cutoff=True)
                    errors.pop("volume", None)
                except (QD35Error, OSError, RuntimeError, TimeoutError, ValueError) as exc:
                    errors["audio"] = str(exc)
                    await stop_audio()
                    player = None

        snooze_cutoff = cutoff
        if manual_event is not None:
            resume_at = datetime.now(UTC) + timedelta(minutes=alarm.physical_snooze_minutes)
            snooze_cutoff = cutoff or wake_cutoff(
                alarm, resume_at + timedelta(minutes=alarm.snooze_fade_minutes)
            )
        if manual_event is not None and snooze_cutoff is not None and resume_at < snooze_cutoff:
            player_exit = await stop_audio()
            night = await apply_night_state(service, alarm)
            if ownership is not None and lease is not None and cutoff is None:
                await ownership.extend_alarm(
                    alarm.classic_device,
                    alarm.alarm_id,
                    snooze_cutoff.timestamp() + AUDIO_CLEANUP_GRACE_SECONDS,
                )
            release_audio = False
            result = {
                "alarm_id": alarm.alarm_id,
                "completed": False,
                "snoozed_by_physical_control": True,
                "snooze_minutes": alarm.physical_snooze_minutes,
                "snooze_cutoff": snooze_cutoff.isoformat(),
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
            result["player_pid"] = player.process.pid
            result["player_exit_code"] = await stop_audio()
            result["post_wake_minutes"] = alarm.post_wake_minutes
            result["fade_out_minutes"] = alarm.fade_out_minutes
        elif alarm.audio_source is not None and "audio" not in errors:
            errors["audio"] = "the scheduled audio window expired before playback started"
        if manual_event is not None:
            result["snooze_skipped"] = "snooze would resume at or after the wake cutoff"
        if alarm.audio_source is not None or manual_event is not None:
            daytime = await restore_daytime_state(service, alarm)
            result["daytime_state"] = daytime
            if "errors" in daytime:
                errors["daytime_state"] = "speaker daytime state was not fully restored"
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
                await stop_audio()
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
    errors: dict[str, str] = {}
    try:
        # Firmware 1.3.0 exposes the A400 control advertisement reliably only
        # after the Classic endpoint is connected. Establish that relationship
        # at bedtime, when an unavoidable device prompt is not a wake-up event.
        if alarm.audio_source is not None:
            try:
                keepalive = await _set_keepalive_enabled(True)
                if keepalive.get("applied") is not True:
                    raise RuntimeError(f"Classic audio keepalive could not start: {keepalive}")
                if ownership is not None:
                    now = datetime.now(UTC)
                    expires_at = (
                        wake_cutoff(alarm, next_wake_time(alarm, now)).timestamp()
                        + AUDIO_CLEANUP_GRACE_SECONDS
                    )
                    await ownership.claim_audio(
                        alarm.classic_device, alarm.alarm_id, expires_at, now.timestamp()
                    )
                if not await _wait_for_classic_connection(alarm.classic_device):
                    raise RuntimeError("Classic audio did not connect during bedtime preparation")
            except (QD35Error, OSError, RuntimeError) as exc:
                errors["audio"] = str(exc)
        # A clock sync failure should not prevent the night lighting from being applied.
        try:
            clock = await service.sync_clock(alarm.clock_format, alarm.timezone)
        except QD35Error as exc:
            clock = {"acknowledged": False, "error": str(exc)}
        night = await apply_night_state(service, alarm)
        errors.update(night["errors"])
        rgb_off = "lighting" not in errors
        display_off = "display" not in errors
        return {
            "alarm_id": alarm.alarm_id,
            "prepared": not errors,
            "brightness": 0 if rgb_off else None,
            "light_rgb": alarm.light_rgb,
            "display_brightness": alarm.night_display_brightness,
            "clock_visible": False if alarm.custom_display and display_off else None,
            "logo_visible": False if alarm.custom_display and display_off else None,
            "symbols_visible": False if alarm.custom_display and display_off else None,
            "display_acknowledged": display_off,
            "clock_acknowledged": clock.get("acknowledged") is True,
            **({"clock_error": clock["error"]} if "error" in clock else {}),
            "classic_audio_touched": keepalive is not None,
            "classic_keepalive": keepalive,
            "bluetooth_disconnect_command_sent": False,
            **({"errors": errors} if errors else {}),
        }
    finally:
        try:
            await service.disconnect()
        finally:
            _restore_environment({"TZ": previous_timezone, **previous_environment})
            if hasattr(time, "tzset"):
                time.tzset()


async def _run_owned(
    alarm: SunriseAlarm,
    *,
    prepare: bool = False,
    scheduled_window: tuple[datetime, datetime] | None = None,
    cutoff: datetime | None = None,
) -> dict[str, object]:
    ownership = SpeakerOwnership()
    lock_timeout = 0.0
    if scheduled_window is not None:
        cutoff = cutoff or wake_cutoff(alarm, scheduled_window[1])
        lock_timeout = min(15, max(0, (cutoff - datetime.now(UTC)).total_seconds()))
    try:
        async with ownership.lock("alarm", alarm.ble_device, timeout=lock_timeout):
            if prepare:
                return await prepare_overnight(alarm, ownership=ownership)
            if cutoff is not None and datetime.now(UTC) >= cutoff:
                return {"alarm_id": alarm.alarm_id, "skipped": "wake window has passed"}
            return await run_alarm(
                alarm, scheduled_window=scheduled_window, ownership=ownership, cutoff=cutoff
            )
    except SpeakerBusy as exc:
        return {"alarm_id": alarm.alarm_id, "completed": False, "error": str(exc)}


async def _retry_bedtime(alarm: SunriseAlarm, deadline: datetime) -> dict[str, object]:
    result: dict[str, object] = {"alarm_id": alarm.alarm_id, "prepared": False}
    delay = 30
    while (remaining := (deadline - datetime.now(UTC)).total_seconds()) > 0:
        try:
            async with asyncio.timeout(remaining):
                result = await _run_owned(alarm, prepare=True)
        except (QD35Error, OSError, RuntimeError) as exc:
            result = {"alarm_id": alarm.alarm_id, "prepared": False, "error": str(exc)}
        if result.get("prepared") is True:
            return result
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        # Hand the speaker lock back before the sunrise unit starts.
        if remaining <= delay + 5:
            break
        print(json.dumps(result, sort_keys=True), file=sys.stderr, flush=True)
        await asyncio.sleep(delay)
        delay = min(300, delay * 2)
    return {**result, "prepared": False, "completed": False, "retries_finished": True}


async def _rearm_physical_snooze(
    timers: SystemdUserTimers, alarm: SunriseAlarm, cutoff: datetime,
) -> None:
    store = AlarmStore(timers.state_path)

    async def schedule() -> None:
        current = store.load().get(alarm.alarm_id)
        if current is None or current.created_at != alarm.created_at:
            return
        await asyncio.to_thread(
            timers.schedule_snooze, alarm.alarm_id, alarm.physical_snooze_minutes, cutoff=cutoff,
        )

    await store.edit(schedule)


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
    timers = SystemdUserTimers(arguments.unit_dir or _alarm_unit_dir(), arguments.state)
    result: dict[str, object]
    if arguments.prepare:
        prepare_timer = f"qd35-alarm-{alarm.alarm_id}-prepare.timer"
        if triggered_by and not in_bedtime_window(alarm, now):
            result = {"alarm_id": alarm.alarm_id, "skipped": "bedtime window has passed"}
        elif triggered_by == prepare_timer:
            deadline = start_for_wake(alarm, next_wake_time(alarm, now)) - timedelta(seconds=5)
            result = asyncio.run(_retry_bedtime(alarm, deadline))
        else:
            result = asyncio.run(_run_owned(alarm, prepare=True))
    elif arguments.snoozed:
        cutoff = timers.snooze_cutoff(alarm.alarm_id)
        if cutoff is None:
            # Older installed snooze timers did not persist a cutoff.
            window = active_wake_window(alarm, now)
            if window is not None:
                cutoff = wake_cutoff(alarm, window[1])
        fade = alarm.snooze_fade_minutes
        if cutoff is not None and now < cutoff:
            result = asyncio.run(
                _run_owned(
                    replace(
                        alarm,
                        sunrise_minutes=fade,
                        audio_fade_minutes=fade,
                        start_brightness=0,
                    ),
                    scheduled_window=(now, min(now + timedelta(minutes=fade), cutoff)),
                    cutoff=cutoff,
                )
            )
        else:
            result = {"alarm_id": alarm.alarm_id, "skipped": "snoozed wake window has passed"}
            if cutoff is not None and alarm.audio_source and not in_bedtime_window(alarm, now):
                result.update(asyncio.run(_finish_missed_snooze(alarm)))
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
        cutoff = datetime.fromisoformat(str(result["snooze_cutoff"]))
        asyncio.run(_rearm_physical_snooze(timers, alarm, cutoff))
    elif result.get("completed") is False or result.get("prepared") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
