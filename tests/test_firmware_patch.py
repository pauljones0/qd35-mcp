from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from qd35_mcp import firmware_patch
from qd35_mcp.firmware_patch import (
    HOOKS,
    IMAGE_BASE,
    PATCH_ADDRESS,
    PATCH_CODE,
    PATCH_LIMIT,
    PATCHED_SHA256,
    STOCK_SHA256,
    STOCK_SIZE,
    patch_file,
    patch_image,
)


def _synthetic_stock() -> bytes:
    data = bytearray(STOCK_SIZE)
    for address, expected, _ in HOOKS:
        offset = address - IMAGE_BASE
        data[offset : offset + len(expected)] = expected
    return bytes(data)


def test_patch_layout_stays_below_animation_storage() -> None:
    assert len(PATCH_CODE) == 452
    assert PATCH_ADDRESS + len(PATCH_CODE) == 0x0800BFE4
    assert PATCH_ADDRESS + len(PATCH_CODE) < PATCH_LIMIT
    assert int.from_bytes(HOOKS[0][2], "little") == 0x20002718


def test_patcher_rejects_wrong_image_even_when_hooks_match() -> None:
    with pytest.raises(ValueError, match="exact supported"):
        patch_image(_synthetic_stock())


def test_published_hashes_have_sha256_shape() -> None:
    assert len(bytes.fromhex(STOCK_SHA256)) == hashlib.sha256().digest_size
    assert len(bytes.fromhex(PATCHED_SHA256)) == hashlib.sha256().digest_size


@pytest.mark.parametrize("alias", ["same path", "symlink", "hard link"])
def test_patch_file_preserves_stock_when_output_aliases_it(tmp_path: Path, alias: str) -> None:
    stock = tmp_path / "stock.bin"
    stock.write_bytes(b"retained stock")
    destination = tmp_path / "patched.bin"
    if alias == "symlink":
        destination.symlink_to(stock)
    elif alias == "hard link":
        os.link(stock, destination)
    else:
        destination = stock

    with pytest.raises(ValueError, match="must be different files"):
        patch_file(stock, destination)

    assert stock.read_bytes() == b"retained stock"


def test_patch_file_replaces_only_after_writing_complete_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stock = tmp_path / "stock.bin"
    stock.write_bytes(b"stock")
    destination = tmp_path / "patched.bin"
    destination.write_bytes(b"existing patch")
    monkeypatch.setattr(firmware_patch, "patch_image", lambda data: data + b" patched")

    def reject_replace(pending: Path, output: Path) -> None:
        assert pending.read_bytes() == b"stock patched"
        assert output.read_bytes() == b"existing patch"
        raise OSError("injected publishing error")

    with monkeypatch.context() as context:
        context.setattr(firmware_patch.os, "replace", reject_replace)
        with pytest.raises(OSError, match="injected publishing error"):
            patch_file(stock, destination)
    assert set(tmp_path.iterdir()) == {stock, destination}
    assert destination.read_bytes() == b"existing patch"

    patch_file(stock, destination)
    assert stock.read_bytes() == b"stock"
    assert destination.read_bytes() == b"stock patched"


def test_assembly_matches_embedded_patch(tmp_path: Path) -> None:
    clang = shutil.which("clang")
    objcopy = shutil.which("llvm-objcopy")
    if not clang or not objcopy:
        pytest.skip("clang and llvm-objcopy are required to check the patch source")
    source = Path(__file__).resolve().parents[1] / "firmware" / "qd35_display_patch.S"
    obj = tmp_path / "patch.o"
    raw = tmp_path / "patch.bin"
    subprocess.run(
        [clang, "--target=arm-none-eabi", "-mcpu=cortex-m3", "-mthumb", "-c", source, "-o", obj],
        check=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    subprocess.run([objcopy, "-O", "binary", "-j", ".text.patch", obj, raw], check=True)
    assert raw.read_bytes() == PATCH_CODE
