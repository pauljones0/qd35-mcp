"""Install and operate systemd user units for the alarm runner."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .alarm_schedule import (
    ALARM_ID_PATTERN,
    SunriseAlarm,
    bedtime_systemd_calendar,
    systemd_calendar,
)
from .errors import TransportError
from .ownership import SpeakerOwnership

KEEPALIVE_UNIT = "qd35-alarm-audio-keepalive.service"
_SNOOZE_CUTOFF_COMMENT = "# QD35 wake cutoff: "


def default_unit_dir() -> Path:
    base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd" / "user"


class SystemdUserTimers:
    def __init__(self, unit_dir: Path, state_path: Path) -> None:
        self.unit_dir = unit_dir
        self.state_path = state_path

    @staticmethod
    def _unit_name(alarm_id: str, suffix: str) -> str:
        base_id = alarm_id.removesuffix("-prepare")
        if not ALARM_ID_PATTERN.fullmatch(base_id) or alarm_id not in {
            base_id,
            f"{base_id}-prepare",
        }:
            raise ValueError("invalid alarm identifier")
        return f"qd35-alarm-{alarm_id}.{suffix}"

    @staticmethod
    def _snooze_unit_name(alarm_id: str, suffix: str) -> str:
        if not ALARM_ID_PATTERN.fullmatch(alarm_id) or suffix not in {"service", "timer"}:
            raise ValueError("invalid snooze unit identifier")
        return f"qd35-alarm-{alarm_id}-snooze.{suffix}"

    @staticmethod
    def _quoted(value: str) -> str:
        return '"' + (
            value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("%", "%%").replace("$", "$$").replace("\n", "\\n").replace("\r", "\\r")
        ) + '"'

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
        state = self._quoted(str(self.state_path.resolve()))
        alarm_id = self._quoted(alarm.alarm_id)
        unit_dir = self._quoted(str(self.unit_dir.resolve()))
        runner = (
            f"{python} -m qd35_mcp.alarm_runner --state {state} "
            f"--alarm {alarm_id} --unit-dir {unit_dir}"
        )
        service = (
            "[Unit]\n"
            f"Description=QD35 sunrise alarm: {alarm.alarm_id}\n"
            "After=bluetooth.service pipewire.service\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={runner}\n"
            "NoNewPrivileges=true\n"
        )
        timer = (
            "[Unit]\n"
            f"Description=Schedule QD35 sunrise alarm: {alarm.alarm_id}\n\n"
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
            f"Description=QD35 snoozed alarm: {alarm.alarm_id}\n"
            "After=bluetooth.service pipewire.service\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={runner} --snoozed\n"
            "NoNewPrivileges=true\n"
        )
        (self.unit_dir / snooze_service_name).write_text(
            snooze_service, encoding="utf-8"
        )
        bedtime_calendar = bedtime_systemd_calendar(alarm)
        prepare_service_name = self._unit_name(f"{alarm.alarm_id}-prepare", "service")
        prepare_timer_name = self._unit_name(f"{alarm.alarm_id}-prepare", "timer")
        if bedtime_calendar is not None:
            prepare_service = (
                "[Unit]\n"
                f"Description=Prepare QD35 overnight light: {alarm.alarm_id}\n"
                "After=bluetooth.service\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                f"ExecStart={runner} --prepare\n"
                "NoNewPrivileges=true\n"
            )
            prepare_timer = (
                "[Unit]\n"
                f"Description=Schedule QD35 overnight light: {alarm.alarm_id}\n\n"
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
        elif (self.unit_dir / prepare_timer_name).exists():
            self._run("disable", "--now", prepare_timer_name)
            self._run("stop", prepare_service_name)
            (self.unit_dir / prepare_timer_name).unlink()
            (self.unit_dir / prepare_service_name).unlink(missing_ok=True)
        self._run("daemon-reload")
        self._run("enable", "--now", timer_name)
        if bedtime_calendar is not None:
            self._run(
                "enable",
                "--now",
                self._unit_name(f"{alarm.alarm_id}-prepare", "timer"),
            )

    def install_keepalive(self, classic_device: str, *, start: bool = True) -> None:
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", classic_device):
            raise ValueError("invalid Classic Bluetooth address")
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        unit_name = KEEPALIVE_UNIT
        python = self._quoted(sys.executable)
        service = (
            "[Unit]\n"
            "Description=Keep QD35 Classic audio connected for alarms\n"
            "After=bluetooth.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={python} -m qd35_mcp.audio_keepalive\n"
            "Restart=always\n"
            "RestartSec=30s\n"
            "NoNewPrivileges=true\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        (self.unit_dir / unit_name).write_text(service, encoding="utf-8")
        self._run("daemon-reload")
        if start:
            self._run("start", unit_name)
            asyncio.run(SpeakerOwnership().set_manual(classic_device, True))

    def remove_keepalive(self) -> None:
        async def clear_overrides() -> None:
            ownership = SpeakerOwnership()
            for address in ownership.audio_addresses():
                await ownership.set_manual(address, False)

        asyncio.run(clear_overrides())

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
        self._run("stop", self._snooze_unit_name(alarm_id, "timer"), check=False)
        self._run("start", self._unit_name(alarm_id, "service"))

    def stop(self, alarm_id: str) -> None:
        self._run("stop", self._snooze_unit_name(alarm_id, "timer"), check=False)
        self._run("stop", self._unit_name(alarm_id, "service"))
        self._run("stop", self._snooze_unit_name(alarm_id, "service"))
        prepare = self._unit_name(f"{alarm_id}-prepare", "service")
        if (self.unit_dir / prepare).exists():
            self._run("stop", prepare)

    def snooze(self, alarm_id: str, minutes: int, *, cutoff: datetime | None = None) -> None:
        if not ALARM_ID_PATTERN.fullmatch(alarm_id) or not 1 <= minutes <= 60:
            raise ValueError("snooze requires a valid alarm and 1-60 minutes")
        self.stop(alarm_id)
        self.schedule_snooze(alarm_id, minutes, cutoff=cutoff)

    def schedule_snooze(
        self, alarm_id: str, minutes: int, *, cutoff: datetime | None = None
    ) -> None:
        """Install a snooze timer without stopping the calling alarm process."""
        if not ALARM_ID_PATTERN.fullmatch(alarm_id) or not 1 <= minutes <= 60:
            raise ValueError("snooze requires a valid alarm and 1-60 minutes")
        timer_name = self._snooze_unit_name(alarm_id, "timer")
        deadline_comment = (
            f"{_SNOOZE_CUTOFF_COMMENT}{cutoff.astimezone(UTC).isoformat()}\n" if cutoff else ""
        )
        timer = (
            f"{deadline_comment}"
            "[Unit]\n"
            f"Description=Snooze QD35 alarm {alarm_id}\n\n"
            "[Timer]\n"
            f"OnActiveSec={minutes}min\n"
            "AccuracySec=1s\n"
            f"Unit={self._snooze_unit_name(alarm_id, 'service')}\n"
        )
        # Keep the resume timer and its original deadline in one atomic update.
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.unit_dir, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            try:
                temporary.write(timer)
                temporary.close()
                temporary_path.replace(self.unit_dir / timer_name)
            finally:
                temporary_path.unlink(missing_ok=True)
        self._run("daemon-reload")
        self._run("restart", timer_name)

    def snooze_cutoff(self, alarm_id: str) -> datetime | None:
        path = self.unit_dir / self._snooze_unit_name(alarm_id, "timer")
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(_SNOOZE_CUTOFF_COMMENT):
                    return datetime.fromisoformat(line.removeprefix(_SNOOZE_CUTOFF_COMMENT))
        except FileNotFoundError:
            return None
        return None

    def state(self, alarm_id: str) -> dict[str, object]:
        timer = self._unit_name(alarm_id, "timer")
        service = self._unit_name(alarm_id, "service")
        enabled = self._run("is-enabled", timer, check=False)
        active = self._run("is-active", service, check=False)
        snooze = self._run(
            "is-active", self._snooze_unit_name(alarm_id, "service"), check=False
        )
        snooze_running = snooze.stdout.strip() == "active"
        return {
            "timer_enabled": enabled.stdout.strip() == "enabled",
            "service_active": active.stdout.strip() == "active" or snooze_running,
            "snooze_service_active": snooze_running,
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
                    "show",
                    self._snooze_unit_name(alarm_id, "timer"),
                    "--property=SubState",
                    "--value",
                    check=False,
                ).stdout.strip()
                == "waiting"
            ),
        }

    def keepalive_state(self) -> dict[str, object]:
        unit_name = KEEPALIVE_UNIT
        active = self._run("is-active", unit_name, check=False)
        ownership = SpeakerOwnership()
        states = [ownership.audio_state(address) for address in ownership.audio_addresses()]
        now = time.time()
        return {
            "enabled": any(state.manual for state in states),
            "active": active.stdout.strip() == "active",
            "scheduled_claims": sum(
                state.lease is not None and state.lease.expires_at > now for state in states
            ),
        }
