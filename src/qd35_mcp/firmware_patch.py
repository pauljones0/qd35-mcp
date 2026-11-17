"""Deterministically apply the open QD35 display patch to a user-supplied image."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from pathlib import Path
from urllib.parse import urlparse

STOCK_SIZE = 40_471
STOCK_SHA256 = "91640d9839e7e0542c5618a0781bc8173915b81a8f2a69173103618993c849b9"
PATCHED_SHA256 = "f58b6cd010f7a3073d8f2f0e50685eb94599f6c57b034e44977984f5f24c6638"
IMAGE_BASE = 0x08002000
PATCH_ADDRESS = 0x0800BE20
PATCH_LIMIT = 0x0800C000
STOCK_URL = (
    "https://edifier-provider-oss.edifier.com/product_firmware/20230221/"
    "2855917202cfc6cbefd62320920c3015.bin"
)

HOOKS = (
    # Effect zero in +LED+SET -> private_command.
    (0x080032E8, bytes.fromhex("c0f00f81"), bytes.fromhex("c8f09a85")),
    # Common CMS1640 buffer writer -> display_write.
    (0x08006874, bytes.fromhex("80b50822"), bytes.fromhex("05f02cbb")),
)

# Exact output of firmware/qd35_display_patch.S assembled with:
# clang --target=arm-none-eabi -mcpu=cortex-m3 -mthumb -c ...
# llvm-objcopy -O binary --only-section=.text.patch ...
PATCH_CODE = bytes.fromhex(
    """
    f0b500f03ef80546607ea17e584b9847352832d1607ca17c554b98470646d02e
    08d0d12e16d0d22e27d10020287000f028f81ce0e07c217d4d4b984700f03f00
    0646607da17d4a4b9847072815d86e70a8700ce0e07c217d454b984707280cd8
    0646607da17d424b98472e44307100f01ff84048012180f87710bde8f040dff8
    f8f03e480178d52911d0d52101700f21417007218170ff21c170394900228b5c
    00eb020c8cf804300132082af7d37047f0b5fff7e6ff0446657815f0200f14bf
    261d2f4e04f10c070022b05cb8540132082afad315f0020f0fd1387800f0f000
    3870787840f03f0078700222b85c00f0c000b8540132082af8d315f0040f0bd1
    387800f09f0038700122b85c00f0bf00b8540132082af8d315f0080f07d10022
    b85c00f07f00b8540132082af8d315f0010f02d1134b9847f0bdc02039460822
    114b984715f0100f09d00a48807c58b10821484301386421b0fbf1f000e0a078
    88300a4b9847f0bd064b9847f0bd00004b2a0008cc130020c539000830170020
    b8160020ef8f0008298f0008cb8f0008
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
    patched = patch_image(source.read_bytes())
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(patched)
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
