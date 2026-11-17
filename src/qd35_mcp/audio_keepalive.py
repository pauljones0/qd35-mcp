"""Maintain the already-paired QD35 Classic audio connection for scheduled alarms."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys


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
        process.kill()
        await process.wait()
        return 124, "command timed out"
    return process.returncode or 0, output.decode("utf-8", "replace")


async def keep_connected(device: str, interval_seconds: float = 30) -> None:
    while True:
        _code, info = await _bluetoothctl("info", device)
        connected = re.search(r"^\s*Connected:\s+yes\s*$", info, re.MULTILINE) is not None
        if not connected:
            code, output = await _bluetoothctl("connect", device)
            summary = output.strip().replace("\n", " | ") or "no output"
            print(
                f"QD35 Classic reconnect exit={code}: {summary}",
                file=sys.stderr,
                flush=True,
            )
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Keep QD35 alarm audio connected")
    parser.add_argument("--device", required=True)
    arguments = parser.parse_args()
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", arguments.device):
        raise SystemExit("invalid Bluetooth address")
    asyncio.run(keep_connected(arguments.device.upper()))


if __name__ == "__main__":
    main()
