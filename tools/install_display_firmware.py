#!/usr/bin/env python3
"""Install the exact QD35 display patch using the recovered Edifier OTA flow."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from qd35_mcp.config import Config
from qd35_mcp.factory import create_service


async def install(source: Path, restore_stock: bool = False) -> dict[str, object]:
    config = Config.from_env()
    service = create_service(config)
    try:
        image = await asyncio.to_thread(source.read_bytes)
        if restore_stock:
            return await service.restore_stock_firmware(image)
        return await service.install_display_firmware(image)
    finally:
        await service.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Patch and install a user-supplied official QD35 MCU 1.3.0 image. Requires "
            "QD35_DEVICE, QD35_ENABLE_WRITES=1, QD35_ENABLE_DANGEROUS=1, and "
            "QD35_ENABLE_FIRMWARE_INSTALL=1. Patch installation also requires "
            "QD35_CUSTOM_DISPLAY_FIRMWARE=1."
        )
    )
    parser.add_argument("source", type=Path, help="official QD35 1.3.0 MCU .bin")
    parser.add_argument(
        "--restore-stock",
        action="store_true",
        help="transfer the exact official image without applying the display patch",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(install(args.source, args.restore_stock)), indent=2))


if __name__ == "__main__":
    main()
