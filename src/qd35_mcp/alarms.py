"""Persist alarm plans and coordinate their lifecycle."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, TypeVar
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .alarm_schedule import (
    AUDIO_CLEANUP_GRACE_SECONDS,
    WEEKDAYS,
    SunriseAlarm,
    active_wake_window,
    alarm_start_schedule,
    bedtime_schedule,
    bedtime_systemd_calendar,
    in_bedtime_window,
    normalize_alarm_id,
    perceptual_light_value,
    ramp_value,
    systemd_calendar,
    validate_weekdays,
    wake_cutoff,
)
from .alarm_systemd import SystemdUserTimers, default_unit_dir
from .config import Config
from .errors import ProtocolError, QD35Error, WritesDisabled
from .factory import create_service
from .ownership import AudioLease, SpeakerOwnership, file_lock

# Keep the original Python imports working for callers using the alarm module.
__all__ = [
    "WEEKDAYS",
    "AlarmManager",
    "AlarmStore",
    "SunriseAlarm",
    "SystemdUserTimers",
    "active_wake_window",
    "alarm_start_schedule",
    "bedtime_schedule",
    "bedtime_systemd_calendar",
    "in_bedtime_window",
    "normalize_alarm_id",
    "perceptual_light_value",
    "ramp_value",
    "systemd_calendar",
    "validate_audio_source",
    "validate_weekdays",
]

_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_T = TypeVar("_T")


def _default_state_path() -> Path:
    base = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "qd35-mcp" / "alarms.json"


def validate_audio_source(source: str | None, config: Config) -> str | None:
    if source is None or not source.strip():
        return None
    value = source.strip()
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        if not config.alarm_remote_audio:
            raise ValueError("remote alarm audio is disabled; set QD35_ALARM_ALLOW_REMOTE_AUDIO=1")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("remote audio URL must have a hostname and no embedded credentials")
        if parsed.hostname.lower() not in config.alarm_allowed_hosts:
            raise ValueError(
                "remote audio hostname is not in QD35_ALARM_ALLOWED_HOSTS"
            )
        try:
            addresses = {
                item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
            }
        except OSError as exc:
            raise ValueError(f"remote audio hostname could not be resolved: {exc}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise ValueError("remote audio URL must resolve only to public addresses")
        return value
    if parsed.scheme == "file":
        candidate = Path(unquote(parsed.path)).resolve()
    elif not parsed.scheme:
        candidate = Path(value).expanduser().resolve()
    else:
        raise ValueError("audio source must be an HTTP(S) URL or local file")
    if config.alarm_media_dir is None:
        raise ValueError("local alarm audio requires QD35_ALARM_MEDIA_DIR")
    media_dir = Path(config.alarm_media_dir).expanduser().resolve()
    if not candidate.is_relative_to(media_dir) or not candidate.is_file():
        raise ValueError("local alarm audio must be an existing file inside QD35_ALARM_MEDIA_DIR")
    return str(candidate)


class AlarmStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    async def edit(self, action: Callable[[], Awaitable[_T]]) -> _T:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        path = self.path.with_name(f"{self.path.name}.lock")
        async with file_lock(path, "alarm schedule", 60):
            async def apply() -> _T:
                return await action()

            change = asyncio.create_task(apply())
            try:
                return await asyncio.shield(change)
            except asyncio.CancelledError:
                # A systemctl command already running in a thread cannot be cancelled.
                while not change.done():
                    try:
                        await asyncio.wait({change})
                    except asyncio.CancelledError:
                        continue
                if not change.cancelled():
                    change.exception()
                raise

    def load(self) -> dict[str, SunriseAlarm]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {key: SunriseAlarm.from_dict(value) for key, value in raw.items()}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProtocolError(f"alarm state is invalid: {exc}") from exc

    def save(self, alarms: dict[str, SunriseAlarm]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {key: alarm.to_dict() for key, alarm in sorted(alarms.items())}
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as temporary:
            path = Path(temporary.name)
            try:
                temporary.write(json.dumps(data, indent=2) + "\n")
                temporary.close()
                path.replace(self.path)
            finally:
                path.unlink(missing_ok=True)


async def restore_daytime_state(service: Any, alarm: SunriseAlarm) -> dict[str, object]:
    """Clear the sunrise lighting and restore the patched display's stock policy."""
    result: dict[str, object] = {"rgb_off": False, "display_reset_acknowledged": None}
    errors: dict[str, str] = {}
    try:
        light = await service.set_light(effect="static", rgb=alarm.light_rgb, brightness=0)
        result["rgb_off"] = light.get("applied") is True
        if not result["rgb_off"]:
            errors["lighting"] = "RGB off was not verified"
    except (QD35Error, RuntimeError) as exc:
        errors["lighting"] = str(exc)
    if alarm.custom_display:
        result["display_reset_acknowledged"] = False
        try:
            display = await service.reset_display()
            result["display_reset_acknowledged"] = display.get("acknowledged") is True
            if not result["display_reset_acknowledged"]:
                errors["display"] = "normal display reset was not acknowledged"
        except (QD35Error, RuntimeError) as exc:
            errors["display"] = str(exc)
    if errors:
        result["errors"] = errors
    return result


async def apply_night_state(service: Any, alarm: SunriseAlarm) -> dict[str, Any]:
    light = display = None
    errors: dict[str, str] = {}
    try:
        light = await service.set_light(effect="static", rgb=alarm.light_rgb, brightness=0)
        if light.get("applied") is not True:
            errors["lighting"] = "RGB off was not verified"
    except (QD35Error, RuntimeError) as exc:
        errors["lighting"] = str(exc)
    if alarm.custom_display:
        try:
            display = await service.set_display(
                power=False, clock=False, logo=False, symbols=False,
                brightness=alarm.night_display_brightness, follow_lighting_dial=False,
                raw_mode=False,
            )
            if display.get("acknowledged") is not True:
                errors["display"] = "display off was not acknowledged"
        except (QD35Error, RuntimeError) as exc:
            errors["display"] = str(exc)
    return {"light": light, "display": display, "errors": errors}


class AlarmManager:
    def __init__(
        self,
        service: Any,
        config: Config,
        store: AlarmStore | None = None,
        timers: SystemdUserTimers | None = None,
    ) -> None:
        state_path = (
            Path(config.alarm_state_path).expanduser()
            if config.alarm_state_path
            else _default_state_path()
        )
        unit_dir = (
            Path(config.alarm_unit_dir).expanduser()
            if config.alarm_unit_dir
            else default_unit_dir()
        )
        self.service = service
        self.config = config
        self.store = store or AlarmStore(state_path)
        self.timers = timers or SystemdUserTimers(unit_dir, state_path)
        self._ownership = SpeakerOwnership() if config.transport == "bleak" else None

    def _require_writes(self) -> None:
        if not self.config.writes_enabled:
            raise WritesDisabled("Alarm writes are disabled; set QD35_ENABLE_WRITES=1")

    def _audio_claim(self, alarm: SunriseAlarm) -> AudioLease | None:
        if self._ownership is not None and alarm.audio_source is not None:
            lease = self._ownership.audio_state(alarm.classic_device).lease
            if lease is not None and lease.alarm_id == alarm.alarm_id:
                return lease
        return None

    @asynccontextmanager
    async def _speaker_for(self, alarm: SunriseAlarm) -> AsyncIterator[Any]:
        if self._ownership is None:
            yield self.service
            return
        async with self._ownership.lock("alarm", alarm.ble_device, timeout=0):
            service = create_service(replace(
                self.config, device=alarm.ble_device, custom_display_firmware=alarm.custom_display,
            ))
            try:
                yield service
            finally:
                await service.disconnect()

    async def create(
        self,
        name: str,
        wake_time: str,
        bedtime: str | None,
        timezone: str,
        weekdays: list[str],
        sunrise_minutes: int,
        start_brightness: int,
        end_brightness: int,
        light_rgb: str,
        clock_format: str,
        audio_source: str | None,
        audio_fade_minutes: int,
        target_speaker_volume: int,
        physical_snooze_minutes: int = 10,
        post_wake_minutes: int = 30,
        fade_out_minutes: int = 2,
    ) -> dict[str, object]:
        self._require_writes()
        alarm_id = normalize_alarm_id(name)
        if alarm_id.endswith(("-prepare", "-snooze")):
            raise ValueError("Alarm names ending in -prepare or -snooze are reserved")
        if not _TIME_PATTERN.fullmatch(wake_time):
            raise ValueError("wake_time must be HH:MM in 24-hour time")
        if bedtime is not None and not _TIME_PATTERN.fullmatch(bedtime):
            raise ValueError("bedtime must be HH:MM in 24-hour time")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {timezone!r}") from exc
        if not 1 <= sunrise_minutes <= 180:
            raise ValueError("sunrise_minutes must be between 1 and 180")
        if not 0 <= start_brightness < end_brightness <= 100:
            raise ValueError("brightness must satisfy 0 <= start < end <= 100")
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", light_rgb):
            raise ValueError("light_rgb must be # followed by six hexadecimal digits")
        if clock_format not in {"preserve", "12h", "24h"}:
            raise ValueError("clock_format must be preserve, 12h, or 24h")
        if not 0 <= audio_fade_minutes <= sunrise_minutes:
            raise ValueError("audio_fade_minutes must be between 0 and sunrise_minutes")
        if not 0 <= target_speaker_volume <= 8:
            raise ValueError("target_speaker_volume must be between 0 and 8 for bedside safety")
        if not 1 <= physical_snooze_minutes <= 60:
            raise ValueError("physical_snooze_minutes must be between 1 and 60")
        if not 0 <= post_wake_minutes <= 180:
            raise ValueError("post_wake_minutes must be between 0 and 180")
        if not 0 <= fade_out_minutes <= 60:
            raise ValueError("fade_out_minutes must be between 0 and 60")
        source = await asyncio.to_thread(validate_audio_source, audio_source, self.config)
        await self.service.set_timezone(timezone, sync_now=False)
        status = await self.service.status()
        device = status["device"]
        classic = status["classic_address"]
        if not isinstance(classic, str) or not isinstance(device, dict):
            raise ProtocolError("speaker identity is unavailable; cannot bind a durable alarm")
        alarm = SunriseAlarm(
            alarm_id=alarm_id,
            name=name.strip(),
            wake_time=wake_time,
            bedtime=bedtime,
            timezone=timezone,
            weekdays=validate_weekdays(weekdays),
            sunrise_minutes=sunrise_minutes,
            start_brightness=start_brightness,
            end_brightness=end_brightness,
            light_rgb=light_rgb.upper(),
            clock_format=clock_format,
            audio_source=source,
            audio_fade_minutes=audio_fade_minutes,
            target_speaker_volume=target_speaker_volume,
            ble_device=str(device["address"]),
            classic_device=classic,
            enabled=True,
            created_at=datetime.now(UTC).isoformat(),
            custom_display=self.config.custom_display_firmware,
            night_display_brightness=0,
            snooze_fade_minutes=2,
            physical_snooze_minutes=physical_snooze_minutes,
            post_wake_minutes=post_wake_minutes,
            fade_out_minutes=fade_out_minutes,
        )
        await self.store.edit(lambda: self._install_plan(alarm))
        return {
            **alarm.to_dict(),
            "schedule": systemd_calendar(alarm),
            "bedtime_schedule": bedtime_systemd_calendar(alarm),
            "installed": True,
        }

    async def _install_plan(self, alarm: SunriseAlarm) -> None:
        alarm_id = alarm.alarm_id
        alarms = self.store.load()
        if any(f"{alarm_id}-{suffix}" in alarms for suffix in ("prepare", "snooze")):
            raise ValueError("Alarm name conflicts with an existing companion unit")
        self.store.save({**alarms, alarm_id: alarm})
        try:
            if alarm.audio_source is not None:
                await asyncio.to_thread(
                    self.timers.install_keepalive, alarm.classic_device, start=False
                )
            await asyncio.to_thread(self.timers.install, alarm)
        except Exception:
            self.store.save(alarms)
            if alarm_id in alarms:
                await asyncio.to_thread(self.timers.install, alarms[alarm_id])
            else:
                await asyncio.to_thread(self.timers.remove, alarm_id)
            raise

    async def list(self) -> dict[str, object]:
        alarms = self.store.load()
        output: list[dict[str, object]] = []
        for alarm in alarms.values():
            state = await asyncio.to_thread(self.timers.state, alarm.alarm_id)
            output.append(
                {
                    **alarm.to_dict(),
                    "schedule": systemd_calendar(alarm),
                    "bedtime_schedule": bedtime_systemd_calendar(alarm),
                    **state,
                }
            )
        keepalive = await asyncio.to_thread(self.timers.keepalive_state)
        return {"alarms": output, "count": len(output), "audio_keepalive": keepalive}

    async def set_audio_keepalive(self, enabled: bool) -> dict[str, object]:
        if enabled:
            self._require_writes()
            status = await self.service.status()
            classic = status["classic_address"]
            if not isinstance(classic, str):
                raise ProtocolError("speaker Classic Bluetooth address is unavailable")
            await asyncio.to_thread(self.timers.install_keepalive, classic)
        else:
            await asyncio.to_thread(self.timers.remove_keepalive)
        state = await asyncio.to_thread(self.timers.keepalive_state)
        return {
            **state,
            "requested": enabled,
            "speaker_disconnect_command_sent": False,
        }

    async def delete(self, alarm_id: str) -> dict[str, object]:
        normalized = normalize_alarm_id(alarm_id)
        return await self.store.edit(lambda: self._delete_alarm(normalized))

    async def _delete_alarm(self, normalized: str) -> dict[str, object]:
        alarms = self.store.load()
        if normalized not in alarms:
            return {"alarm_id": normalized, "deleted": False}
        alarm = alarms[normalized]
        await asyncio.to_thread(self.timers.remove, normalized)
        alarms.pop(normalized)
        self.store.save(alarms)
        claim = self._audio_claim(alarm)
        if self._ownership is not None and claim is not None:
            await self._ownership.cancel_alarm(alarm.classic_device, claim)
        return {"alarm_id": normalized, "deleted": True}

    async def start_now(self, alarm_id: str) -> dict[str, object]:
        self._require_writes()
        normalized = normalize_alarm_id(alarm_id)
        return await self.store.edit(lambda: self._start_now(normalized))

    async def _start_now(self, normalized: str) -> dict[str, object]:
        if normalized not in self.store.load():
            raise ValueError(f"alarm {normalized!r} does not exist")
        state = await asyncio.to_thread(self.timers.state, normalized)
        if state.get("service_active"):
            return {"alarm_id": normalized, "started": False, "already_active": True}
        await asyncio.to_thread(self.timers.start_now, normalized)
        return {"alarm_id": normalized, "started": True}

    async def stop(self, alarm_id: str) -> dict[str, object]:
        normalized = normalize_alarm_id(alarm_id)
        return await self.store.edit(lambda: self._stop_alarm(normalized))

    async def _stop_alarm(self, normalized: str) -> dict[str, object]:
        result: dict[str, object] = {
            "alarm_id": normalized, "stopped": True, "bluetooth_disconnected": False,
        }
        alarm = self.store.load().get(normalized)
        if alarm is None:
            raise ValueError(f"alarm {normalized!r} does not exist")
        state = await asyncio.to_thread(self.timers.state, normalized)
        await asyncio.to_thread(self.timers.stop, normalized)
        claim = self._audio_claim(alarm)
        try:
            was_running = state.get("service_active") or state.get("snooze_active")
            if self.config.writes_enabled and was_running:
                async with self._speaker_for(alarm) as service:
                    result["daytime_state"] = await restore_daytime_state(service, alarm)
        except QD35Error as exc:
            result["daytime_state_error"] = str(exc)
        finally:
            if self._ownership is not None and claim is not None:
                await self._ownership.cancel_alarm(alarm.classic_device, claim)
        return result

    async def snooze(self, alarm_id: str, minutes: int = 10) -> dict[str, object]:
        self._require_writes()
        normalized = normalize_alarm_id(alarm_id)
        if not 1 <= minutes <= 60:
            raise ValueError("snooze minutes must be between 1 and 60")
        return await self.store.edit(lambda: self._snooze_locked(normalized, minutes))

    async def _snooze_locked(self, normalized: str, minutes: int) -> dict[str, object]:
        alarms = self.store.load()
        if normalized not in alarms:
            raise ValueError(f"alarm {normalized!r} does not exist")
        state = await asyncio.to_thread(self.timers.state, normalized)
        if not (state.get("service_active") or state.get("snooze_active")):
            return {"alarm_id": normalized, "snoozed": False, "reason": "alarm is idle"}
        alarm = alarms[normalized]
        now = datetime.now(UTC)
        resume_at = now + timedelta(minutes=minutes)
        window = active_wake_window(alarm, now)
        cutoff = None
        if state.get("snooze_service_active") or state.get("snooze_active"):
            cutoff = await asyncio.to_thread(self.timers.snooze_cutoff, normalized)
        if cutoff is None:
            cutoff = wake_cutoff(alarm, window[1]) if window else wake_cutoff(
                alarm, resume_at + timedelta(minutes=alarm.snooze_fade_minutes)
            )
        will_resume = resume_at < cutoff
        if will_resume:
            await asyncio.to_thread(self.timers.snooze, normalized, minutes, cutoff=cutoff)
        else:
            await asyncio.to_thread(self.timers.stop, normalized)
        disposition = {
            "alarm_id": normalized,
            "snoozed": will_resume,
            "resume_at": resume_at.isoformat() if will_resume else None,
            "cutoff": cutoff.isoformat(),
        }
        try:
            async with self._speaker_for(alarm) as service:
                standalone_audio = alarm.audio_source and will_resume and window is None
                if self._ownership is not None and standalone_audio:
                    await self._ownership.extend_alarm(
                        alarm.classic_device, alarm.alarm_id,
                        cutoff.timestamp() + AUDIO_CLEANUP_GRACE_SECONDS,
                    )
                if not will_resume:
                    return {
                        **disposition,
                        "reason": "snooze would resume at or after the wake cutoff",
                        "daytime_state": await restore_daytime_state(service, alarm),
                        "bluetooth_disconnected": False,
                    }
                night = await apply_night_state(service, alarm)
        except QD35Error as exc:
            return {**disposition, "speaker_state_skipped": str(exc)}
        return {
            **disposition,
            "minutes": minutes,
            "night_light_applied": "lighting" not in night["errors"],
            "night_display_acknowledged": "display" not in night["errors"],
            **({"night_state_errors": night["errors"]} if night["errors"] else {}),
            "bluetooth_disconnected": False,
        }
