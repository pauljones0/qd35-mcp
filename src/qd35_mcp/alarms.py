"""Persistent sunrise-alarm plans backed by systemd user timers."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .errors import ProtocolError, TransportError

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SYSTEMD_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _default_state_path() -> Path:
    base = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "qd35-mcp" / "alarms.json"


def _default_unit_dir() -> Path:
    base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd" / "user"


@dataclass(frozen=True, slots=True)
class SunriseAlarm:
    alarm_id: str
    name: str
    wake_time: str
    bedtime: str | None
    timezone: str
    weekdays: tuple[str, ...]
    sunrise_minutes: int
    start_brightness: int
    end_brightness: int
    light_rgb: str
    clock_format: str
    audio_source: str | None
    audio_fade_minutes: int
    target_speaker_volume: int
    ble_device: str
    classic_device: str
    enabled: bool
    created_at: str
    custom_display: bool = False
    night_display_brightness: int = 0
    snooze_fade_minutes: int = 2

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SunriseAlarm:
        return cls(
            alarm_id=str(value["alarm_id"]),
            name=str(value["name"]),
            wake_time=str(value["wake_time"]),
            bedtime=str(value["bedtime"]) if value.get("bedtime") else None,
            timezone=str(value["timezone"]),
            weekdays=tuple(str(day) for day in value["weekdays"]),
            sunrise_minutes=int(value["sunrise_minutes"]),
            start_brightness=int(value["start_brightness"]),
            end_brightness=int(value["end_brightness"]),
            light_rgb=str(value["light_rgb"]),
            clock_format=str(value.get("clock_format", "12h")),
            audio_source=str(value["audio_source"]) if value.get("audio_source") else None,
            audio_fade_minutes=int(value["audio_fade_minutes"]),
            target_speaker_volume=int(value["target_speaker_volume"]),
            ble_device=str(value["ble_device"]),
            classic_device=str(value["classic_device"]),
            enabled=bool(value["enabled"]),
            created_at=str(value["created_at"]),
            custom_display=bool(value.get("custom_display", False)),
            night_display_brightness=int(value.get("night_display_brightness", 0)),
            snooze_fade_minutes=int(value.get("snooze_fade_minutes", 2)),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_alarm_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-")
    if not _ID_PATTERN.fullmatch(normalized):
        raise ValueError("alarm name must produce a 1-48 character identifier")
    return normalized


def validate_weekdays(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise ValueError("weekdays must contain at least one day")
    normalized = tuple(dict.fromkeys(day.strip().lower() for day in values))
    if any(day not in WEEKDAYS for day in normalized):
        raise ValueError(f"weekdays must use: {', '.join(WEEKDAYS)}")
    return tuple(day for day in WEEKDAYS if day in normalized)


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


def alarm_start_schedule(alarm: SunriseAlarm) -> tuple[str, tuple[str, ...]]:
    hour, minute = (int(part) for part in alarm.wake_time.split(":"))
    start_total = hour * 60 + minute - alarm.sunrise_minutes
    day_shift = -1 if start_total < 0 else 0
    start_total %= 24 * 60
    shifted: list[str] = []
    for day in alarm.weekdays:
        index = WEEKDAYS.index(day)
        shifted.append(WEEKDAYS[(index + day_shift) % 7])
    ordered = tuple(day for day in WEEKDAYS if day in shifted)
    return f"{start_total // 60:02d}:{start_total % 60:02d}", ordered


def systemd_calendar(alarm: SunriseAlarm) -> str:
    start_time, start_days = alarm_start_schedule(alarm)
    days = ",".join(SYSTEMD_DAYS[WEEKDAYS.index(day)] for day in start_days)
    return f"{days} *-*-* {start_time}:00 {alarm.timezone}"


def bedtime_schedule(alarm: SunriseAlarm) -> tuple[str, tuple[str, ...]] | None:
    if alarm.bedtime is None:
        return None
    bedtime_hour, bedtime_minute = (int(part) for part in alarm.bedtime.split(":"))
    wake_hour, wake_minute = (int(part) for part in alarm.wake_time.split(":"))
    bedtime_total = bedtime_hour * 60 + bedtime_minute
    wake_total = wake_hour * 60 + wake_minute
    day_shift = -1 if bedtime_total > wake_total else 0
    ordered = tuple(
        dict.fromkeys(
            WEEKDAYS[(WEEKDAYS.index(day) + day_shift) % 7]
            for day in alarm.weekdays
        )
    )
    return alarm.bedtime, ordered


def bedtime_systemd_calendar(alarm: SunriseAlarm) -> str | None:
    schedule = bedtime_schedule(alarm)
    if schedule is None:
        return None
    bedtime, days_value = schedule
    days = ",".join(SYSTEMD_DAYS[WEEKDAYS.index(day)] for day in days_value)
    return f"{days} *-*-* {bedtime}:00 {alarm.timezone}"


def ramp_value(start: int, end: int, progress: float) -> int:
    bounded = min(1.0, max(0.0, progress))
    return round(start + (end - start) * bounded)


class AlarmStore:
    def __init__(self, path: Path) -> None:
        self.path = path

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
        temporary = self.path.with_suffix(".tmp")
        data = {key: alarm.to_dict() for key, alarm in sorted(alarms.items())}
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)


class SystemdUserTimers:
    def __init__(self, unit_dir: Path, state_path: Path) -> None:
        self.unit_dir = unit_dir
        self.state_path = state_path

    @staticmethod
    def _unit_name(alarm_id: str, suffix: str) -> str:
        base_id = alarm_id.removesuffix("-prepare")
        if not _ID_PATTERN.fullmatch(base_id) or alarm_id not in {
            base_id,
            f"{base_id}-prepare",
        }:
            raise ValueError("invalid alarm identifier")
        return f"qd35-alarm-{alarm_id}.{suffix}"

    @staticmethod
    def _snooze_unit_name(alarm_id: str, suffix: str) -> str:
        if not _ID_PATTERN.fullmatch(alarm_id) or suffix not in {"service", "timer"}:
            raise ValueError("invalid snooze unit identifier")
        return f"qd35-alarm-{alarm_id}-snooze.{suffix}"

    @staticmethod
    def _quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if check and result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown systemctl error"
            raise TransportError(detail)
        return result

    def install(self, alarm: SunriseAlarm) -> None:
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        service_name = self._unit_name(alarm.alarm_id, "service")
        timer_name = self._unit_name(alarm.alarm_id, "timer")
        python = self._quoted(sys.executable)
        state = self._quoted(str(self.state_path))
        alarm_id = self._quoted(alarm.alarm_id)
        service = (
            "[Unit]\n"
            f"Description=QD35 sunrise alarm: {alarm.name}\n"
            "After=bluetooth.service pipewire.service\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={python} -m qd35_mcp.alarm_runner --state {state} --alarm {alarm_id}\n"
            "NoNewPrivileges=true\n"
        )
        timer = (
            "[Unit]\n"
            f"Description=Schedule QD35 sunrise alarm: {alarm.name}\n\n"
            "[Timer]\n"
            f"OnCalendar={systemd_calendar(alarm)}\n"
            "Persistent=true\n"
            "AccuracySec=1s\n"
            f"Unit={service_name}\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        (self.unit_dir / service_name).write_text(service, encoding="utf-8")
        (self.unit_dir / timer_name).write_text(timer, encoding="utf-8")
        snooze_service_name = self._snooze_unit_name(alarm.alarm_id, "service")
        snooze_service = (
            "[Unit]\n"
            f"Description=QD35 snoozed alarm: {alarm.name}\n"
            "After=bluetooth.service pipewire.service\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={python} -m qd35_mcp.alarm_runner --state {state} "
            f"--alarm {alarm_id} --snoozed\n"
            "NoNewPrivileges=true\n"
        )
        (self.unit_dir / snooze_service_name).write_text(
            snooze_service, encoding="utf-8"
        )
        bedtime_calendar = bedtime_systemd_calendar(alarm)
        if bedtime_calendar is not None:
            prepare_service_name = self._unit_name(
                f"{alarm.alarm_id}-prepare", "service"
            )
            prepare_timer_name = self._unit_name(f"{alarm.alarm_id}-prepare", "timer")
            prepare_service = (
                "[Unit]\n"
                f"Description=Prepare QD35 overnight light: {alarm.name}\n"
                "After=bluetooth.service\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                f"ExecStart={python} -m qd35_mcp.alarm_runner --state {state} "
                f"--alarm {alarm_id} --prepare\n"
                "NoNewPrivileges=true\n"
            )
            prepare_timer = (
                "[Unit]\n"
                f"Description=Schedule QD35 overnight light: {alarm.name}\n\n"
                "[Timer]\n"
                f"OnCalendar={bedtime_calendar}\n"
                "Persistent=true\n"
                "AccuracySec=1s\n"
                f"Unit={prepare_service_name}\n\n"
                "[Install]\n"
                "WantedBy=timers.target\n"
            )
            (self.unit_dir / prepare_service_name).write_text(
                prepare_service, encoding="utf-8"
            )
            (self.unit_dir / prepare_timer_name).write_text(
                prepare_timer, encoding="utf-8"
            )
        self._run("daemon-reload")
        self._run("enable", "--now", timer_name)
        if bedtime_calendar is not None:
            self._run(
                "enable",
                "--now",
                self._unit_name(f"{alarm.alarm_id}-prepare", "timer"),
            )

    def install_keepalive(self, classic_device: str) -> None:
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", classic_device):
            raise ValueError("invalid Classic Bluetooth address")
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        unit_name = "qd35-alarm-audio-keepalive.service"
        python = self._quoted(sys.executable)
        device = self._quoted(classic_device.upper())
        service = (
            "[Unit]\n"
            "Description=Keep QD35 Classic audio connected for alarms\n"
            "After=bluetooth.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={python} -m qd35_mcp.audio_keepalive --device {device}\n"
            "Restart=always\n"
            "RestartSec=30s\n"
            "NoNewPrivileges=true\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        (self.unit_dir / unit_name).write_text(service, encoding="utf-8")
        self._run("daemon-reload")
        self._run("enable", "--now", unit_name)

    def remove_keepalive(self) -> None:
        unit_name = "qd35-alarm-audio-keepalive.service"
        self._run("disable", "--now", unit_name, check=False)
        path = self.unit_dir / unit_name
        if path.exists():
            path.unlink()
        self._run("daemon-reload")

    def remove(self, alarm_id: str) -> None:
        service_name = self._unit_name(alarm_id, "service")
        timer_name = self._unit_name(alarm_id, "timer")
        self._run("disable", "--now", timer_name, check=False)
        self._run("stop", service_name, check=False)
        prepare_service_name = self._unit_name(f"{alarm_id}-prepare", "service")
        prepare_timer_name = self._unit_name(f"{alarm_id}-prepare", "timer")
        self._run("disable", "--now", prepare_timer_name, check=False)
        self._run("stop", prepare_service_name, check=False)
        snooze_service_name = self._snooze_unit_name(alarm_id, "service")
        snooze_timer_name = self._snooze_unit_name(alarm_id, "timer")
        self._run("stop", snooze_timer_name, check=False)
        self._run("stop", snooze_service_name, check=False)
        for name in (
            service_name,
            timer_name,
            prepare_service_name,
            prepare_timer_name,
            snooze_service_name,
            snooze_timer_name,
        ):
            path = self.unit_dir / name
            if path.exists():
                path.unlink()
        self._run("daemon-reload")

    def start_now(self, alarm_id: str) -> None:
        self._run("start", self._unit_name(alarm_id, "service"))

    def stop(self, alarm_id: str) -> None:
        self._run("stop", self._unit_name(alarm_id, "service"), check=False)
        self._run("stop", self._snooze_unit_name(alarm_id, "timer"), check=False)
        self._run("stop", self._snooze_unit_name(alarm_id, "service"), check=False)

    def snooze(self, alarm_id: str, minutes: int) -> None:
        if not _ID_PATTERN.fullmatch(alarm_id) or not 1 <= minutes <= 60:
            raise ValueError("snooze requires a valid alarm and 1-60 minutes")
        self.stop(alarm_id)
        timer_name = self._snooze_unit_name(alarm_id, "timer")
        timer = (
            "[Unit]\n"
            f"Description=Snooze QD35 alarm {alarm_id}\n\n"
            "[Timer]\n"
            f"OnActiveSec={minutes}min\n"
            "AccuracySec=1s\n"
            f"Unit={self._snooze_unit_name(alarm_id, 'service')}\n"
        )
        (self.unit_dir / timer_name).write_text(timer, encoding="utf-8")
        self._run("daemon-reload")
        self._run("start", timer_name)

    def state(self, alarm_id: str) -> dict[str, object]:
        timer = self._unit_name(alarm_id, "timer")
        service = self._unit_name(alarm_id, "service")
        enabled = self._run("is-enabled", timer, check=False)
        active = self._run("is-active", service, check=False)
        return {
            "timer_enabled": enabled.stdout.strip() == "enabled",
            "service_active": active.stdout.strip() == "active",
            "preparation_timer_enabled": (
                self._run(
                    "is-enabled",
                    self._unit_name(f"{alarm_id}-prepare", "timer"),
                    check=False,
                ).stdout.strip()
                == "enabled"
            ),
            "snooze_active": (
                self._run(
                    "is-active",
                    self._snooze_unit_name(alarm_id, "timer"),
                    check=False,
                ).stdout.strip()
                == "active"
            ),
        }

    def keepalive_state(self) -> dict[str, object]:
        unit_name = "qd35-alarm-audio-keepalive.service"
        enabled = self._run("is-enabled", unit_name, check=False)
        active = self._run("is-active", unit_name, check=False)
        return {
            "enabled": enabled.stdout.strip() == "enabled",
            "active": active.stdout.strip() == "active",
        }


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
            else _default_unit_dir()
        )
        self.service = service
        self.config = config
        self.store = store or AlarmStore(state_path)
        self.timers = timers or SystemdUserTimers(unit_dir, state_path)

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
    ) -> dict[str, object]:
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
        source = validate_audio_source(audio_source, self.config)
        await self.service.set_timezone(timezone, sync_now=False)
        status = await self.service.status()
        device = status["device"]
        classic = status["classic_address"]
        if not isinstance(classic, str) or not isinstance(device, dict):
            raise ProtocolError("speaker identity is unavailable; cannot bind a durable alarm")
        alarm_id = normalize_alarm_id(name)
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
        )
        alarms = self.store.load()
        previous = alarms.get(alarm_id)
        alarms[alarm_id] = alarm
        self.store.save(alarms)
        try:
            await asyncio.to_thread(self.timers.install, alarm)
            if alarm.audio_source is not None:
                await asyncio.to_thread(self.timers.install_keepalive, alarm.classic_device)
        except Exception:
            await asyncio.to_thread(self.timers.remove, alarm_id)
            if previous is None:
                alarms.pop(alarm_id, None)
            else:
                alarms[alarm_id] = previous
            self.store.save(alarms)
            raise
        return {
            **alarm.to_dict(),
            "schedule": systemd_calendar(alarm),
            "bedtime_schedule": bedtime_systemd_calendar(alarm),
            "installed": True,
        }

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
        alarms = self.store.load()
        existed = normalized in alarms
        await asyncio.to_thread(self.timers.remove, normalized)
        alarms.pop(normalized, None)
        self.store.save(alarms)
        if not any(alarm.audio_source is not None for alarm in alarms.values()):
            await asyncio.to_thread(self.timers.remove_keepalive)
        return {"alarm_id": normalized, "deleted": existed}

    async def start_now(self, alarm_id: str) -> dict[str, object]:
        normalized = normalize_alarm_id(alarm_id)
        if normalized not in self.store.load():
            raise ValueError(f"alarm {normalized!r} does not exist")
        await asyncio.to_thread(self.timers.start_now, normalized)
        return {"alarm_id": normalized, "started": True}

    async def stop(self, alarm_id: str) -> dict[str, object]:
        normalized = normalize_alarm_id(alarm_id)
        await asyncio.to_thread(self.timers.stop, normalized)
        return {"alarm_id": normalized, "stopped": True, "bluetooth_disconnected": False}

    async def snooze(self, alarm_id: str, minutes: int = 10) -> dict[str, object]:
        normalized = normalize_alarm_id(alarm_id)
        alarms = self.store.load()
        if normalized not in alarms:
            raise ValueError(f"alarm {normalized!r} does not exist")
        if not 1 <= minutes <= 60:
            raise ValueError("snooze minutes must be between 1 and 60")
        await asyncio.to_thread(self.timers.snooze, normalized, minutes)
        alarm = alarms[normalized]
        light = await self.service.set_light(
            effect="static",
            rgb=alarm.light_rgb,
            brightness=0,
        )
        display: dict[str, object] | None = None
        if alarm.custom_display:
            display = await self.service.set_display(
                power=False,
                clock=False,
                logo=False,
                symbols=False,
                brightness=alarm.night_display_brightness,
                follow_lighting_dial=False,
                raw_mode=False,
            )
        return {
            "alarm_id": normalized,
            "snoozed": True,
            "minutes": minutes,
            "resume_at": (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(),
            "night_light_applied": light.get("applied") is True,
            "night_display_acknowledged": (
                display is None or display.get("acknowledged") is True
            ),
            "bluetooth_disconnected": False,
        }
