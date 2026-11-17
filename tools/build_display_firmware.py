#!/usr/bin/env python3
"""Build a display-control image from an exact, user-supplied QD35 1.3.0 image."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from qd35_mcp.firmware_patch import patch_file


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the QD35 display patch without redistributing Edifier firmware."
    )
    parser.add_argument("source", type=Path, help="official QD35 1.3.0 MCU .bin")
    parser.add_argument("destination", type=Path, help="output path for the patched .bin")
    args = parser.parse_args()
    print(json.dumps(patch_file(args.source, args.destination), indent=2))


if __name__ == "__main__":
    main()
