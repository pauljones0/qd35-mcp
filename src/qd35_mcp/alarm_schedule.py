"""Alarm plans and pure calendar/ramp calculations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SYSTEMD_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ALARM_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


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
    physical_snooze_minutes: int = 10
    post_wake_minutes: int = 30

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
            physical_snooze_minutes=int(value.get("physical_snooze_minutes", 10)),
            post_wake_minutes=int(value.get("post_wake_minutes", 30)),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_alarm_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-")
    if not ALARM_ID_PATTERN.fullmatch(normalized):
        raise ValueError("alarm name must produce a 1-48 character identifier")
    return normalized


def validate_weekdays(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise ValueError("weekdays must contain at least one day")
    normalized = tuple(dict.fromkeys(day.strip().lower() for day in values))
    if any(day not in WEEKDAYS for day in normalized):
        raise ValueError(f"weekdays must use: {', '.join(WEEKDAYS)}")
    return tuple(day for day in WEEKDAYS if day in normalized)


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
    day_shift = (wake_total - alarm.sunrise_minutes - bedtime_total - 1) // (24 * 60)
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


def _local_time(day: date, clock: str, zone: ZoneInfo) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(UTC)


def active_wake_window(alarm: SunriseAlarm, now: datetime) -> tuple[datetime, datetime] | None:
    """Return the scheduled ramp start and wake time if this timer can still catch up."""
    zone = ZoneInfo(alarm.timezone)
    day = now.astimezone(zone).date()
    start_time, _ = alarm_start_schedule(alarm)
    start_day_shift = -1 if start_time > alarm.wake_time else 0
    after_wake = timedelta(minutes=alarm.post_wake_minutes if alarm.audio_source else 0)
    for offset in (-1, 0, 1):
        wake_day = day + timedelta(days=offset)
        if WEEKDAYS[wake_day.weekday()] not in alarm.weekdays:
            continue
        wake = _local_time(wake_day, alarm.wake_time, zone)
        start = _local_time(wake_day + timedelta(days=start_day_shift), start_time, zone)
        if start <= now < wake + after_wake:
            return start, wake
    return None


def next_wake_time(alarm: SunriseAlarm, now: datetime) -> datetime:
    zone = ZoneInfo(alarm.timezone)
    day = now.astimezone(zone).date()
    for offset in range(8):
        candidate_day = day + timedelta(days=offset)
        if WEEKDAYS[candidate_day.weekday()] in alarm.weekdays:
            wake = _local_time(candidate_day, alarm.wake_time, zone)
            if wake > now:
                return wake
    raise ValueError("Alarm has no upcoming wake day")


def in_bedtime_window(alarm: SunriseAlarm, now: datetime) -> bool:
    bedtime = bedtime_schedule(alarm)
    if bedtime is None:
        return False
    bedtime_time, bedtime_days = bedtime
    start_time, start_days = alarm_start_schedule(alarm)
    zone = ZoneInfo(alarm.timezone)
    day = now.astimezone(zone).date()

    def last_occurrence(clock: str, days: tuple[str, ...]) -> datetime | None:
        for offset in range(8):
            candidate_day = day - timedelta(days=offset)
            if WEEKDAYS[candidate_day.weekday()] not in days:
                continue
            candidate = _local_time(candidate_day, clock, zone)
            if candidate <= now:
                return candidate
        return None

    last_bedtime = last_occurrence(bedtime_time, bedtime_days)
    last_start = last_occurrence(start_time, start_days)
    return last_bedtime is not None and (last_start is None or last_start < last_bedtime)


def ramp_value(start: int, end: int, progress: float) -> int:
    bounded = min(1.0, max(0.0, progress))
    return round(start + (end - start) * bounded)


def perceptual_light_value(
    start: int, end: int, progress: float, *, gamma: float = 2.2
) -> int:
    """Map linear elapsed time to an approximately linear perceived-light ramp.

    QD35 brightness is a linear 0-100 control, while perceived brightness is
    nonlinear.  Raising normalized progress to a display-style gamma keeps the
    physical output low early in the sunrise and avoids an apparent jump at the
    beginning.  The endpoints remain exact, including a true zero/off state.
    """
    if gamma <= 0:
        raise ValueError("light gamma must be positive")
    bounded = min(1.0, max(0.0, progress))
    corrected = bounded**gamma
    return round(start + (end - start) * corrected)
