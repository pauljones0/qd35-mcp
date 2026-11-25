"""Same-user, cross-process coordination for BLE operations and Classic audio."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from .errors import SpeakerBusy, TransportError

_ADDRESS = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")


def _runtime_dir() -> Path:
    # Prefer the canonical per-user runtime directory even for GUI clients whose
    # environment lacks XDG_RUNTIME_DIR. On systems without it, use the home dir.
    runtime = Path(f"/run/user/{os.getuid()}")
    return (runtime if runtime.is_dir() else Path.home() / ".cache") / "qd35-mcp"


@asynccontextmanager
async def file_lock(path: Path, kind: str, timeout: float) -> AsyncIterator[None]:
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise TransportError(f"Cannot open the QD35 {kind} lock: {exc}") from exc
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise SpeakerBusy(f"DEVICE_BUSY: another process owns QD35 {kind}") from exc
                await asyncio.sleep(min(0.05, remaining))
        yield
    finally:
        # Never unlink lock files. Waiters must continue to share the same inode.
        os.close(fd)


@dataclass(frozen=True, slots=True)
class AudioLease:
    alarm_id: str
    token: str
    expires_at: float
    reconnect_at: float


@dataclass(frozen=True, slots=True)
class AudioState:
    lease: AudioLease | None = None
    manual: bool = False


class SpeakerOwnership:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _runtime_dir()

    def _path(self, address: str, kind: str, suffix: str) -> Path:
        address = address.upper()
        if not _ADDRESS.fullmatch(address):
            raise TransportError("Speaker ownership requires a Bluetooth MAC address")
        return self.root / f"{kind}-{address}.{suffix}"

    @asynccontextmanager
    async def lock(
        self, kind: str, address: str, *, timeout: float = 5
    ) -> AsyncIterator[None]:
        path = self._path(address, kind, "lock")
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if self.root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError("ownership directory must be private to the current user")
        except OSError as exc:
            raise TransportError(f"Cannot open the QD35 {kind} lock: {exc}") from exc
        async with file_lock(path, kind, timeout):
            yield

    def audio_addresses(self) -> list[str]:
        return [path.stem.removeprefix("audio-") for path in self.root.glob("audio-*.json")]

    def audio_state(self, address: str) -> AudioState:
        path = self._path(address, "audio", "json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return AudioState(AudioLease(**raw["lease"]) if raw["lease"] else None, raw["manual"])
        except FileNotFoundError:
            return AudioState()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise TransportError(f"Cannot read QD35 audio ownership: {exc}") from exc

    def save_audio_state(self, address: str, state: AudioState) -> None:
        """Replace the record while holding this address's audio lock."""
        path = self._path(address, "audio", "json")
        if state == AudioState():
            path.unlink(missing_ok=True)
            return
        with NamedTemporaryFile(mode="w", dir=self.root, delete=False) as temporary:
            temp_path = Path(temporary.name)
            try:
                json.dump(asdict(state), temporary)
                temporary.close()
                temp_path.replace(path)
            finally:
                temp_path.unlink(missing_ok=True)

    async def claim_audio(
        self, address: str, alarm_id: str, expires_at: float, reconnect_at: float
    ) -> AudioLease:
        async with self.lock("audio", address):
            state = self.audio_state(address)
            lease = state.lease
            now = time.time()
            if lease and lease.expires_at > now:
                if lease.alarm_id != alarm_id:
                    raise SpeakerBusy(f"ALARM_ACTIVE: {lease.alarm_id} owns QD35 audio")
                reconnect_at = min(reconnect_at, lease.reconnect_at)
            claim = AudioLease(alarm_id, uuid4().hex, expires_at, reconnect_at)
            self.save_audio_state(address, replace(state, lease=claim))
            return claim

    async def extend_alarm(self, address: str, alarm_id: str, expires_at: float) -> None:
        async with self.lock("audio", address):
            state = self.audio_state(address)
            if state.lease and state.lease.alarm_id == alarm_id:
                lease = replace(state.lease, expires_at=max(expires_at, state.lease.expires_at))
                self.save_audio_state(address, replace(state, lease=lease))

    async def set_manual(self, address: str, enabled: bool) -> None:
        async with self.lock("audio", address):
            self.save_audio_state(address, replace(self.audio_state(address), manual=enabled))

    async def cancel_alarm(self, address: str, claim: AudioLease) -> None:
        """Stop reconnecting for a cancelled alarm without forcing an audio disconnect."""
        async with self.lock("audio", address):
            state = self.audio_state(address)
            if state.lease is not None and state.lease.token == claim.token:
                self.save_audio_state(address, replace(state, lease=None))

    async def release_audio(
        self,
        address: str,
        claim: AudioLease,
        disconnect: Callable[[str], Awaitable[bool]],
        *,
        expired_only: bool = False,
    ) -> str:
        # Hold the lock through the Bluetooth side effect. Comparing tokens
        # before releasing the lock would let a new claim race the disconnect.
        async with self.lock("audio", address):
            state = self.audio_state(address)
            lease = state.lease
            if lease is None or lease.token != claim.token:
                return "not_owner"
            if expired_only and lease.expires_at > time.time():
                return "unexpired"
            if state.manual or time.time() < lease.reconnect_at:
                self.save_audio_state(address, replace(state, lease=None))
                return "manual_keepalive" if state.manual else "audio_not_due"
            if not await disconnect(address):
                return "disconnect_failed"
            self.save_audio_state(address, replace(state, lease=None))
            return "disconnected"
