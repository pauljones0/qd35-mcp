"""Maintain the already-paired QD35 Classic audio connection for scheduled alarms."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from contextlib import suppress

from .errors import QD35Error
from .ownership import SpeakerOwnership


async def _bluetoothctl(*arguments: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 20)
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
    return process.returncode or 0, output.decode("utf-8", "replace")


async def _reconnect(device: str) -> None:
    _code, info = await _bluetoothctl("info", device)
    if re.search(r"^\s*Connected:\s+yes\s*$", info, re.MULTILINE):
        return
    code, output = await _bluetoothctl("connect", device)
    summary = output.strip().replace("\n", " | ") or "no output"
    print(f"QD35 Classic reconnect exit={code}: {summary}", file=sys.stderr, flush=True)


async def disconnect_classic(device: str) -> bool:
    code, output = await _bluetoothctl("disconnect", device)
    if code == 0 and "successful" in output.lower():
        return True
    code, info = await _bluetoothctl("info", device)
    return code == 0 and "Connected: no" in info


async def maintain_device(ownership: SpeakerOwnership, device: str) -> None:
    async with ownership.lock("audio", device, timeout=0):
        state = ownership.audio_state(device)
        lease = state.lease
        now = time.time()
        if lease is None or lease.expires_at > now:
            if state.manual:
                await _reconnect(device)
            elif lease is not None and lease.reconnect_at <= now:
                # Cancel a still-running connect before the lease can expire.
                with suppress(TimeoutError):
                    await asyncio.wait_for(_reconnect(device), lease.expires_at - now)
            return
    await ownership.release_audio(device, lease, disconnect_classic, expired_only=True)


async def keep_connected(ownership: SpeakerOwnership, interval_seconds: float = 5) -> None:
    while True:
        for device in ownership.audio_addresses():
            try:
                await maintain_device(ownership, device)
            except QD35Error as exc:
                print(f"QD35 keepalive: {exc}", file=sys.stderr, flush=True)
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Keep QD35 alarm audio connected")
    # Accept the argument used by installed units. The owner records now select devices.
    parser.add_argument("--device", help=argparse.SUPPRESS)
    parser.parse_args()
    asyncio.run(keep_connected(SpeakerOwnership()))


if __name__ == "__main__":
    main()
