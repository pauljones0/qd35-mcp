"""Persistent IANA timezone resolution for speaker clock synchronization."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def default_timezone_state_path() -> Path:
    base = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "qd35-mcp" / "timezone"


def validate_timezone(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("timezone cannot be empty")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone {normalized!r}") from exc
    return normalized


def detect_system_timezone() -> str:
    environment = (os.getenv("TZ") or "").strip().lstrip(":")
    if environment:
        try:
            return validate_timezone(environment)
        except ValueError:
            pass
    timezone_file = Path("/etc/timezone")
    if timezone_file.is_file():
        try:
            return validate_timezone(timezone_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
    try:
        target = Path("/etc/localtime").resolve()
        marker = "/zoneinfo/"
        if marker in str(target):
            return validate_timezone(str(target).split(marker, 1)[1])
    except (OSError, ValueError):
        pass
    return "UTC"


def resolve_timezone(configured: str | None, state_path: Path) -> str:
    if configured is not None:
        return validate_timezone(configured)
    if state_path.is_file():
        try:
            return validate_timezone(state_path.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    return detect_system_timezone()


def persist_timezone(value: str, state_path: Path) -> str:
    normalized = validate_timezone(value)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(normalized + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(state_path)
    return normalized
