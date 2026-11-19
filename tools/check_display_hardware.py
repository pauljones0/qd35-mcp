#!/usr/bin/env python3
"""Cycle QD35 display isolation frames for a fixed-exposure camera recording."""

from __future__ import annotations

import argparse
import asyncio
import select
import sys

from qd35_mcp.config import Config
from qd35_mcp.factory import create_service

FRAMES = (
    ("driver off", None),
    ("eight zero bytes with driver on", bytes(8)),
    ("clock digits only", bytes.fromhex("00 1e 19 03 1f 3d 27 1f")),
    ("stock horizontal-line group", bytes.fromhex("60 40 40 40 40 40 40 40")),
    ("second startup group", bytes.fromhex("00 00 00 00 00 00 80 80")),
    ("driver off again", None),
)


def wait_for_step() -> bool:
    print("Waiting for Enter to advance (three-minute timeout).", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], 180)
    return bool(ready and sys.stdin.readline() == "\n")


async def record(
    brightness: int, hold_seconds: float, repeat: int, step: bool, logo_pwm: bool,
) -> None:
    async def hold() -> bool:
        if step:
            return await asyncio.to_thread(wait_for_step)
        await asyncio.sleep(hold_seconds)
        return True

    config = Config.from_env()
    if not (
        config.device and config.writes_enabled
        and config.custom_display_firmware and config.dangerous_writes_enabled
    ):
        raise SystemExit(
            "Set QD35_DEVICE, QD35_ENABLE_WRITES=1, QD35_ENABLE_DANGEROUS=1, "
            "and QD35_CUSTOM_DISPLAY_FIRMWARE=1"
        )
    service = create_service(config)
    try:
        status = await service.status()
        lighting = status["lighting"]
        selected = lighting["selected_effect"]
        original = next(effect for effect in lighting["effects"] if effect["name"] == selected)
        try:
            if (await service.set_light(brightness=0))["applied"] is not True:
                raise RuntimeError("Could not confirm that ordinary RGB lighting is off")
            if logo_pwm:
                await service.set_display(
                    power=False, clock=True, logo=True, symbols=True,
                    brightness=0, follow_lighting_dial=False, raw_mode=False,
                )
                for index in range(8):
                    await service.write_display_grid_byte(index, 0)
                await service.set_display(power=True, raw_mode=True)
                print("Logo PWM test: eight zero bytes, driver on at 0/7.", flush=True)
                if await hold():
                    await service.set_display(brightness=7)
                    print("Logo PWM test: same bytes, brightness now 7/7.", flush=True)
                    await hold()
                return
            await service.set_display(
                power=True, clock=True, logo=False, symbols=False,
                brightness=brightness, follow_lighting_dial=False, raw_mode=False,
            )
            loop = asyncio.get_running_loop()
            started = loop.time()
            print("  0.00s  start marker: clock on, logo and symbols masked", flush=True)
            if not await hold():
                return
            for lap in range(1, repeat + 1):
                for label, frame in FRAMES:
                    await service.set_display(power=False, raw_mode=False)
                    if frame is not None:
                        for index, value in enumerate(frame):
                            await service.write_display_grid_byte(index, value)
                        await service.set_display(
                            power=True, logo=True, symbols=True, raw_mode=True,
                        )
                    print(f"{loop.time() - started:6.2f}s  {lap}/{repeat}  {label}", flush=True)
                    if not await hold():
                        return
        finally:
            try:
                await service.reset_display()
            finally:
                restored = await service.set_light(
                    effect=selected, brightness=original["brightness"]
                )
                if restored["applied"] is not True:
                    raise RuntimeError("Could not confirm that RGB settings were restored")
    finally:
        await service.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brightness", type=int, choices=range(8), default=2)
    parser.add_argument("--hold-seconds", type=float, default=5)
    parser.add_argument("--repeat", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--step", action="store_true", help="Wait for Enter between phases")
    parser.add_argument(
        "--logo-pwm", action="store_true", help="Compare display PWM with all exposed bytes zero",
    )
    args = parser.parse_args()
    if not 1 <= args.hold_seconds <= 30:
        parser.error("--hold-seconds must be between 1 and 30")
    asyncio.run(record(args.brightness, args.hold_seconds, args.repeat, args.step, args.logo_pwm))


if __name__ == "__main__":
    main()
