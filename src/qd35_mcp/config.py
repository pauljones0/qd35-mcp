from __future__ import annotations

import os
from dataclasses import dataclass
from math import isfinite


def _enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: 1/0, true/false, yes/no, on/off")


@dataclass(frozen=True, slots=True)
class Config:
    device: str | None
    adapter: str | None
    transport: str
    writes_enabled: bool
    dangerous_writes_enabled: bool
    request_timeout: float
    scan_timeout: float
    alarm_state_path: str | None = None
    alarm_media_dir: str | None = None
    alarm_unit_dir: str | None = None
    alarm_remote_audio: bool = False
    alarm_allowed_hosts: tuple[str, ...] = ()
    timezone: str | None = None
    timezone_state_path: str | None = None

    def __post_init__(self) -> None:
        if self.transport not in {"bleak", "fake"}:
            raise ValueError("transport must be 'bleak' or 'fake'")
        if not isfinite(self.request_timeout) or not 0 < self.request_timeout <= 60:
            raise ValueError("request_timeout must be greater than 0 and at most 60 seconds")
        if not isfinite(self.scan_timeout) or not 0 < self.scan_timeout <= 300:
            raise ValueError("scan_timeout must be greater than 0 and at most 300 seconds")

    @classmethod
    def from_env(cls) -> Config:
        transport = os.getenv("QD35_TRANSPORT", "bleak").strip().lower()
        if transport not in {"bleak", "fake"}:
            raise ValueError("QD35_TRANSPORT must be 'bleak' or 'fake'")
        return cls(
            device=(os.getenv("QD35_DEVICE") or "").strip() or None,
            adapter=(os.getenv("QD35_ADAPTER") or "").strip() or None,
            transport=transport,
            writes_enabled=_enabled("QD35_ENABLE_WRITES", transport == "fake"),
            dangerous_writes_enabled=_enabled("QD35_ENABLE_DANGEROUS"),
            request_timeout=float(os.getenv("QD35_REQUEST_TIMEOUT", "4")),
            scan_timeout=float(os.getenv("QD35_SCAN_TIMEOUT", "8")),
            alarm_state_path=(os.getenv("QD35_ALARM_STATE_PATH") or "").strip() or None,
            alarm_media_dir=(os.getenv("QD35_ALARM_MEDIA_DIR") or "").strip() or None,
            alarm_unit_dir=(os.getenv("QD35_ALARM_UNIT_DIR") or "").strip() or None,
            alarm_remote_audio=_enabled("QD35_ALARM_ALLOW_REMOTE_AUDIO"),
            alarm_allowed_hosts=tuple(
                host.strip().lower()
                for host in os.getenv("QD35_ALARM_ALLOWED_HOSTS", "").split(",")
                if host.strip()
            ),
            timezone=(os.getenv("QD35_TIMEZONE") or "").strip() or None,
            timezone_state_path=(os.getenv("QD35_TIMEZONE_STATE_PATH") or "").strip()
            or None,
        )
