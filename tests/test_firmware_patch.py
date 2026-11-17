from __future__ import annotations

import hashlib

import pytest

from qd35_mcp.firmware_patch import (
    HOOKS,
    IMAGE_BASE,
    PATCH_ADDRESS,
    PATCH_CODE,
    PATCH_LIMIT,
    PATCHED_SHA256,
    STOCK_SHA256,
    STOCK_SIZE,
    patch_image,
)


def _synthetic_stock() -> bytes:
    data = bytearray(STOCK_SIZE)
    for address, expected, _ in HOOKS:
        offset = address - IMAGE_BASE
        data[offset : offset + len(expected)] = expected
    return bytes(data)


def test_patch_layout_stays_below_animation_storage() -> None:
    assert len(PATCH_CODE) == 400
    assert PATCH_ADDRESS + len(PATCH_CODE) == 0x0800BFB0
    assert PATCH_ADDRESS + len(PATCH_CODE) < PATCH_LIMIT


def test_patcher_rejects_wrong_image_even_when_hooks_match() -> None:
    with pytest.raises(ValueError, match="exact supported"):
        patch_image(_synthetic_stock())


def test_published_hashes_have_sha256_shape() -> None:
    assert len(bytes.fromhex(STOCK_SHA256)) == hashlib.sha256().digest_size
    assert len(bytes.fromhex(PATCHED_SHA256)) == hashlib.sha256().digest_size
