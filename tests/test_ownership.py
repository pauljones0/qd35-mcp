from __future__ import annotations

import asyncio
import sys
import time

import pytest

from qd35_mcp import alarm_runner, audio_keepalive
from qd35_mcp import constants as c
from qd35_mcp.config import Config
from qd35_mcp.errors import SpeakerBusy
from qd35_mcp.ownership import SpeakerOwnership
from qd35_mcp.service import QD35Service
from qd35_mcp.transport.fake import FakeTransport

BLE = "FA:CE:D3:50:00:01"
CLASSIC = "AA:BB:CC:DD:EE:FF"


async def test_process_crash_releases_flock(tmp_path):
    ownership = SpeakerOwnership(tmp_path / "runtime")
    script = """
import asyncio, sys
from pathlib import Path
from qd35_mcp.ownership import SpeakerOwnership

async def main():
    async with SpeakerOwnership(Path(sys.argv[1])).lock('ble', sys.argv[2]):
        print('locked', flush=True)
        await asyncio.Event().wait()

asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(ownership.root), BLE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"locked\n"
        with pytest.raises(SpeakerBusy, match="DEVICE_BUSY"):
            async with ownership.lock("ble", BLE.lower(), timeout=0):
                pytest.fail("two processes acquired the same BLE lock")
        async with ownership.lock("ble", CLASSIC, timeout=0):
            pass
    finally:
        process.kill()
        await process.wait()
    async with ownership.lock("ble", BLE, timeout=0):
        pass


async def test_ble_lock_spans_readback_and_auto_target_uses_same_lock(tmp_path):
    ownership = SpeakerOwnership(tmp_path / "runtime")
    reading_back = asyncio.Event()
    finish_readback = asyncio.Event()
    second_request = asyncio.Event()

    class FirstTransport(FakeTransport):
        async def request(self, opcode, payload=b"", timeout=4):
            if opcode == c.QUERY_VOLUME and self.volume == 12:
                reading_back.set()
                await finish_readback.wait()
            return await super().request(opcode, payload, timeout)

    class SecondTransport(FakeTransport):
        async def request(self, opcode, payload=b"", timeout=4):
            second_request.set()
            return await super().request(opcode, payload, timeout)

    first = QD35Service(FirstTransport(), Config(BLE, None, "fake", True, False, 1, 1), ownership)
    second = QD35Service(
        SecondTransport(), Config(None, None, "fake", True, False, 1, 1), ownership
    )
    first_set = asyncio.create_task(first.set_volume(12))
    try:
        await asyncio.wait_for(reading_back.wait(), 2)
        second_set = asyncio.create_task(second.set_volume(3))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_request.wait(), 0.1)
    finally:
        finish_readback.set()
        assert (await first_set)["applied"] is True
    assert (await second_set)["applied"] is True


async def test_old_cleanup_cannot_disconnect_a_new_owner(tmp_path):
    ownership = SpeakerOwnership(tmp_path / "runtime")
    now = time.time()
    old = await ownership.claim_audio(CLASSIC, "morning", now + 90, now)
    with pytest.raises(SpeakerBusy, match="morning owns"):
        await ownership.claim_audio(CLASSIC, "other", now + 90, now)
    new = await ownership.claim_audio(CLASSIC, "morning", now + 120, now)
    disconnected = []

    async def disconnect(address):
        disconnected.append(address)
        return True

    assert await ownership.release_audio(CLASSIC, old, disconnect) == "not_owner"
    await ownership.cancel_alarm(CLASSIC, old)
    assert disconnected == []
    assert ownership.audio_state(CLASSIC).lease == new
    assert await ownership.release_audio(CLASSIC, new, disconnect) == "disconnected"
    assert disconnected == [CLASSIC]


async def test_worker_disconnects_expired_owner_and_never_reconnects(tmp_path, monkeypatch):
    ownership = SpeakerOwnership(tmp_path / "runtime")
    now = time.time()
    script = """
import asyncio, os, sys
from pathlib import Path
from qd35_mcp.ownership import SpeakerOwnership

now = float(sys.argv[3])
ownership = SpeakerOwnership(Path(sys.argv[1]))
asyncio.run(ownership.claim_audio(sys.argv[2], 'crashed', now + 60, now))
os._exit(17)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(ownership.root), CLASSIC, str(now)
    )
    assert await asyncio.wait_for(process.wait(), 5) == 17
    assert ownership.audio_state(CLASSIC).lease.alarm_id == "crashed"
    monkeypatch.setattr(time, "time", lambda: now + 61)
    calls = []

    async def command(*args):
        calls.append(args)
        return 0, "Disconnection successful"

    monkeypatch.setattr(audio_keepalive, "_bluetoothctl", command)
    await audio_keepalive.maintain_device(ownership, CLASSIC)
    await audio_keepalive.maintain_device(ownership, CLASSIC)
    assert calls == [("disconnect", CLASSIC)]
    assert ownership.audio_addresses() == []


async def test_keepalive_waits_for_audio_and_respects_manual_override(tmp_path, monkeypatch):
    ownership = SpeakerOwnership(tmp_path / "runtime")
    now = time.time()
    lease = await ownership.claim_audio(CLASSIC, "morning", now + 120, now + 60)

    async def unexpected(*args):
        pytest.fail(f"Bluetooth touched unexpectedly: {args}")

    monkeypatch.setattr(audio_keepalive, "_bluetoothctl", unexpected)
    await audio_keepalive.maintain_device(ownership, CLASSIC)
    await ownership.set_manual(CLASSIC, True)
    outcome = await ownership.release_audio(CLASSIC, lease, unexpected)
    assert outcome == "manual_keepalive"
    assert ownership.audio_state(CLASSIC).manual


async def test_overlapping_runner_is_blocked_before_touching_the_speaker(tmp_path, monkeypatch):
    from dataclasses import dataclass

    @dataclass
    class Plan:
        ble_device: str = BLE
        alarm_id: str = "second"

    ownership = SpeakerOwnership(tmp_path / "runtime")

    def unexpected(*args, **kwargs):
        pytest.fail("a second alarm tried to create the speaker service")

    monkeypatch.setattr(alarm_runner, "SpeakerOwnership", lambda: ownership)
    monkeypatch.setattr(alarm_runner, "create_service", unexpected)
    async with ownership.lock("alarm", BLE):
        result = await alarm_runner._run_owned(Plan())
    assert result["completed"] is False
    assert "DEVICE_BUSY" in result["error"]


async def test_scheduled_wake_can_wait_for_a_bedtime_cleanup(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from test_alarm_runner import alarm

    ownership = SpeakerOwnership(tmp_path / "runtime")
    plan = alarm(ble_device=BLE)
    now = datetime.now(UTC)

    async def run(*_args, **_kwargs):
        return {"completed": True}

    monkeypatch.setattr(alarm_runner, "SpeakerOwnership", lambda: ownership)
    monkeypatch.setattr(alarm_runner, "run_alarm", run)
    async with ownership.lock("alarm", BLE):
        wake = asyncio.create_task(alarm_runner._run_owned(
            plan, scheduled_window=(now, now + timedelta(minutes=1)),
        ))
        await asyncio.sleep(0.02)
        assert not wake.done()
    assert await asyncio.wait_for(wake, 1) == {"completed": True}
