"""Deterministically apply the open QD35 display patch to a user-supplied image."""

from __future__ import annotations

import asyncio
import hashlib
import os
import ssl
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

STOCK_SIZE = 40_471
STOCK_SHA256 = "91640d9839e7e0542c5618a0781bc8173915b81a8f2a69173103618993c849b9"
PATCHED_SHA256 = "b2354c561dd02e73e8d8f25599b956d69e58742421f8edc13101a3f7f0786c8a"
IMAGE_BASE = 0x08002000
PATCH_ADDRESS = 0x0800BE20
PATCH_LIMIT = 0x0800C000
STOCK_URL = (
    "https://edifier-provider-oss.edifier.com/product_firmware/20230221/"
    "2855917202cfc6cbefd62320920c3015.bin"
)

HOOKS = (
    # Shorten the downward-growing stack by 24 bytes for patch state.
    (0x08002000, bytes.fromhex("30270020"), bytes.fromhex("18270020")),
    # Effect zero in +LED+SET -> private_command.
    (0x080032E8, bytes.fromhex("c0f00f81"), bytes.fromhex("c8f09a85")),
    # Common CMS1640 buffer writer -> display_write.
    (0x08006874, bytes.fromhex("80b50822"), bytes.fromhex("05f038bb")),
    # Alternate stock framebuffer draw uses the same masks and power policy.
    (0x08006E2C, bytes.fromhex("02f07cf8"), bytes.fromhex("05f05df8")),
    # The RAM write no longer implicitly enables maximum brightness.
    (0x08008F7A, bytes.fromhex("fff77cff"), bytes.fromhex("00f005b8")),
    # Stock wake/reinitialization display control uses the same policy.
    (0x08008FCA, bytes.fromhex("10b50400"), bytes.fromhex("02f0d2bf")),
    # Reset clears the private state even when SRAM is retained.
    (0x0800B2F4, bytes.fromhex("01488047"), bytes.fromhex("00f059be")),
)

# Exact output of firmware/qd35_display_patch.S assembled with:
# clang --target=arm-none-eabi -mcpu=cortex-m3 -mthumb -c ...
# llvm-objcopy -O binary --only-section=.text.patch ...
PATCH_CODE = bytes.fromhex(
    """
    f8b500f055f80546192000f04cf8352845d1112000f047f80746d02f0c
    d0d32f0ad0d12f2ad0d42f35d0d22f37d10020287000f03df82ce01320
    00f034f800f03f000646152000f02ef8072827d8d32f02d06e70a8701c
    e00746172000f023f87f281cd810f0400f18bfaf7000f03f0069788143
    0640314369700ae0132000f012f807280bd80646152000f00cf82e4430
    7100f019f84148012180f87710bde8f840dff8fcf0421ca15c205c3d4b
    18473d480178d52906d03c4901603c4991e80c00c0e90123704739498c
    463348017911b190f86d1001b17047f8b56646fff7e7ff0446657815f0
    200f18bf261d04f10c070022b05cb8540132082afad33878ff2115f002
    0f04bf00f0f00001f0c00115f0040f04bf00f09f0001f0bf0115f0080f
    04bf00f06f0001f07f0138700122b85c0840b8540132082af9d315f001
    0f04d0c020394608221a4b984700f001f8f8bd10b5fff7acff04466278
    12f0010f0fd012f0100f09d00c48807c48b10821484301386421b0fbf1
    f000e0a078883000e0802004460c4b1847074880f3088800210170094b
    9847094b1847cc130020c53900084b2a000818270020d50f0700b81600
    20298f0008cf8f000879ab0008f9b20008
    """
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_image(stock: bytes) -> bytes:
    """Return a patched image after fail-closed stock-image validation."""
    if len(stock) != STOCK_SIZE or sha256(stock) != STOCK_SHA256:
        raise ValueError(
            "input is not the exact supported QD35 MCU 1.3.0 image "
            f"(expected {STOCK_SIZE} bytes, SHA-256 {STOCK_SHA256})"
        )
    output = bytearray(stock)
    for address, expected, replacement in HOOKS:
        offset = address - IMAGE_BASE
        if output[offset : offset + len(expected)] != expected:
            raise ValueError(f"firmware hook bytes differ at 0x{address:08X}")
        output[offset : offset + len(expected)] = replacement
    patch_offset = PATCH_ADDRESS - IMAGE_BASE
    if PATCH_ADDRESS + len(PATCH_CODE) > PATCH_LIMIT:
        raise AssertionError("display patch overlaps animation storage")
    if len(output) < patch_offset:
        # Raw firmware images omit trailing erased flash. Preserve the physical
        # erased-state value rather than introducing programmed zero bytes.
        output.extend(b"\xFF" * (patch_offset - len(output)))
    output[patch_offset : patch_offset + len(PATCH_CODE)] = PATCH_CODE
    result = bytes(output)
    if sha256(result) != PATCHED_SHA256:
        raise AssertionError("deterministic patched-image hash mismatch")
    return result


def patch_file(source: Path, destination: Path) -> dict[str, object]:
    output = destination.resolve()
    if source.resolve() == output or (output.exists() and source.samefile(output)):
        raise ValueError("stock firmware and patched output must be different files")
    patched = patch_image(source.read_bytes())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = NamedTemporaryFile(dir=output.parent, delete=False)
    pending = Path(temporary.name)
    try:
        with temporary:
            temporary.write(patched)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(pending, output)
    finally:
        pending.unlink(missing_ok=True)
    return {
        "source": str(source),
        "destination": str(destination),
        "size": len(patched),
        "sha256": sha256(patched),
        "patch_address": f"0x{PATCH_ADDRESS:08X}",
        "animation_storage_untouched_from": f"0x{PATCH_LIMIT:08X}",
    }


async def download_stock_image() -> bytes:
    """Fetch only the pinned public Edifier image with one bounded HTTPS exchange."""
    parsed = urlparse(STOCK_URL)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise AssertionError("stock firmware URL must be an absolute HTTPS URL")
    context = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                parsed.hostname,
                443,
                ssl=context,
                server_hostname=parsed.hostname,
            ),
            timeout=15,
        )
    except TimeoutError as exc:
        raise ValueError("timed out connecting to the pinned Edifier firmware host") from exc
    try:
        request = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}\r\n"
            "User-Agent: qd35-mcp/0.1\r\n"
            "Accept: application/octet-stream\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        if len(header) > 65_536:
            raise ValueError("Edifier firmware response headers are too large")
        lines = header.decode("iso-8859-1").split("\r\n")
        status = lines[0].split()
        if len(status) < 2 or status[1] != "200":
            raise ValueError(f"Edifier firmware download returned {lines[0]}")
        headers = {
            key.strip().lower(): value.strip()
            for line in lines[1:]
            if ":" in line
            for key, value in [line.split(":", 1)]
        }
        if headers.get("transfer-encoding", "").lower() == "chunked":
            raise ValueError("chunked firmware responses are not accepted")
        if "content-length" in headers and int(headers["content-length"]) != STOCK_SIZE:
            raise ValueError("Edifier firmware response has the wrong declared size")
        data = await asyncio.wait_for(reader.readexactly(STOCK_SIZE), timeout=15)
        extra = await asyncio.wait_for(reader.read(1), timeout=15)
        if extra:
            raise ValueError("Edifier firmware response exceeds the pinned image size")
    finally:
        writer.close()
        await writer.wait_closed()
    if len(data) != STOCK_SIZE or sha256(data) != STOCK_SHA256:
        raise ValueError("downloaded Edifier firmware does not match the pinned 1.3.0 image")
    return data
