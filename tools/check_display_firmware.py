#!/usr/bin/env python3
"""Exercise the exact patched image offline: uv run --with unicorn python ... stock.bin."""

from __future__ import annotations

import argparse
from pathlib import Path
from struct import pack

from unicorn import UC_ARCH_ARM, UC_HOOK_CODE, UC_MODE_MCLASS, UC_MODE_THUMB, Uc
from unicorn.arm_const import (
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_R0,
    UC_ARM_REG_R4,
    UC_ARM_REG_R5,
    UC_ARM_REG_SP,
)

from qd35_mcp.animations import FRAME_SIZE, LIGHT_COUNT, build_creative_animation
from qd35_mcp.firmware_patch import IMAGE_BASE, patch_image

STATE = 0x20002718
OLD_SP = 0x20002730
GLOBAL = 0x200013CC
DISPLAY_BUF = 0x200016B8
COMMAND = 0x20001000
RETURN = 0x08000100
REDRAW = 0x08006874
PARSER_TAIL = 0x080039C4
FRAME = bytes.fromhex("aa 12 34 56 78 9a bc de")
DIGITS_ONLY = bytes.fromhex("00 1e 19 03 1f 3d 27 1f")


class DisplayEmulator:
    def __init__(self, image: bytes) -> None:
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_MCLASS | UC_MODE_THUMB)
        self.cpu.mem_map(0x08000000, 0x10000)
        self.cpu.mem_write(IMAGE_BASE, image)
        self.cpu.mem_map(0x20000000, 0x10000)
        self.cpu.hook_add(UC_HOOK_CODE, self._hook)
        self.wire = bytearray()
        self.stop = RETURN

    def _hook(self, cpu: Uc, address: int, _size: int, _data: object) -> None:
        if address == self.stop:
            cpu.emu_stop()
        # Substitute only the bitbang primitives and reset's hardware setup.
        elif address in (0x08008E76, 0x08008EA8, 0x08008EDA, 0x0800AB78):
            if address == 0x08008EDA:
                self.wire.append(cpu.reg_read(UC_ARM_REG_R0) & 0xFF)
            cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    def enter(self, address: int, stop: int = RETURN, stack: int = STATE) -> bytes:
        self.stop = stop
        self.wire.clear()
        self.cpu.reg_write(UC_ARM_REG_SP, stack)
        self.cpu.reg_write(UC_ARM_REG_LR, RETURN | 1)
        self.cpu.emu_start(address | 1, 0, count=50_000)
        assert self.cpu.reg_read(UC_ARM_REG_PC) == stop
        assert self.cpu.reg_read(UC_ARM_REG_SP) == STATE
        return bytes(self.wire)

    def seed(self, flags: int, brightness: int, rgb: int = 20) -> None:
        self.cpu.mem_write(STATE, bytes((0xD5, flags, brightness, 0)) + FRAME)
        self.cpu.mem_write(DISPLAY_BUF, FRAME)
        self.cpu.mem_write(GLOBAL + 4, b"\x01")
        self.cpu.mem_write(GLOBAL + 0x6D, b"\x00")
        self.cpu.mem_write(GLOBAL + 0x12, bytes((rgb,)))

    def private(self, op: int, arg: int = 0, value: int = 0, mask: int = 0) -> bytes:
        cmd = bytearray(b"0" * 32)
        for offset, number in ((0x11, op), (0x13, arg), (0x15, value), (0x17, mask), (0x19, 0x35)):
            cmd[offset : offset + 2] = f"{number:02X}".encode()
        self.cpu.mem_write(COMMAND, bytes(cmd))
        self.cpu.mem_write(GLOBAL + 0x77, b"\x00")
        self.cpu.reg_write(UC_ARM_REG_R4, COMMAND)
        return self.enter(0x080032E0, PARSER_TAIL)


def exercise(stock: bytes) -> None:
    em = DisplayEmulator(patch_image(stock))
    for flags in range(64):
        for brightness in range(8):
            # Disabled, minimum and maximum ordinary RGB levels cover follow-mode endpoints.
            for rgb in (0, 1, 100):
                em.seed(flags, brightness, rgb)
                wire = em.enter(REDRAW)
                level = 0x80 if not flags & 1 else 0x88 + brightness
                if flags & 1 and flags & 16:
                    level = 0x88 + (rgb * 8 - 1) // 100 if rgb else 0x80
                if flags & 1:
                    assert wire[:2] == b"\x40\xc0" and len(wire) == 12
                else:
                    assert len(wire) == 2
                assert wire[-2:] == bytes((0x40, level))

    for offset, value in ((4, 0), (0x6D, 1)):
        for flags in (0, 15, 31):
            em.seed(flags, 0)
            em.cpu.mem_write(GLOBAL + offset, bytes((value,)))
            assert em.enter(REDRAW) == b""

    for mask in range(128):
        em.seed(0b101010, 5)
        em.private(0xD3, 0b010101, 2, mask)
        flags = (0b101010 & ~mask) | (0b010101 & mask)
        assert em.cpu.mem_read(STATE + 1, 2) == bytes((flags, 2 if mask & 64 else 5))
        assert em.cpu.mem_read(GLOBAL + 0x77, 1) == b"\x01"

    em.seed(15, 7)
    assert em.private(0xD3, 15, 8, 64) == b""
    assert em.cpu.mem_read(GLOBAL + 0x77, 1) == b"\x00"
    em.private(0xD0, 15, 3)
    for index in range(8):
        em.private(0xD1, index, 255 - index)
    assert em.cpu.mem_read(STATE + 4, 8) == bytes(range(255, 247, -1))
    em.private(0xD2)
    assert em.cpu.mem_read(STATE + 1, 2) == b"\x0f\x07"
    assert em.cpu.mem_read(STATE + 4, 8) == FRAME

    # Replay the recorded bedside frame through the real parser and RAM writer.
    for index, value in enumerate(DIGITS_ONLY):
        em.private(0xD1, index, value)
    assert em.private(0xD0, 0x23, 0) == b"\x40\xc0" + DIGITS_ONLY + b"\x40\x88"
    em.cpu.mem_write(DISPLAY_BUF, b"\xff" + bytes(v | 0xC0 for v in DIGITS_ONLY[1:]))
    assert em.private(0xD0, 0x03, 0) == b"\x40\xc0\x0f" + DIGITS_ONLY[1:] + b"\x40\x88"
    state_before_probe = em.cpu.mem_read(STATE, 20)
    rgb_before_probe = em.cpu.mem_read(GLOBAL + 0x12, 1)
    assert em.private(0xD4, mask=11) == b""
    assert em.cpu.mem_read(GLOBAL + 0x77, 1) == b"\x01"
    assert em.cpu.mem_read(STATE, 20) == state_before_probe
    assert em.cpu.mem_read(GLOBAL + 0x12, 1) == rgb_before_probe

    stock_display = DisplayEmulator(stock)
    stock_display.seed(15, 7, 10)
    assert stock_display.private(0xD4, mask=11) == b""
    assert stock_display.cpu.mem_read(GLOBAL + 0x77, 1) == b"\x01"
    assert stock_display.cpu.mem_read(GLOBAL + 0x12, 1) == b"\x0b"

    assert em.private(0xD5) == b""
    assert em.cpu.mem_read(GLOBAL + 0x77, 1) == b"\x00"
    for index in range(8):
        em.private(0xD1, index, 0)
    assert em.private(0xD0, 0x2F, 0) == b"\x40\xc0" + bytes(8) + b"\x40\x88"
    assert em.private(0xD3, 0, 7, 0x40) == b"\x40\xc0" + bytes(8) + b"\x40\x8f"
    assert em.private(0xD0, 0x22, 0) == b"\x40\x80"

    em.seed(0, 0)
    em.cpu.reg_write(UC_ARM_REG_R0, 1)
    em.cpu.reg_write(UC_ARM_REG_R5, 0x12345678)
    wire = em.enter(0x08006D50)
    assert wire.startswith(b"\x40\xc0" + bytes(16) + b"\x40\x80")
    assert all(value != 0x8F for value in wire)
    assert em.cpu.reg_read(UC_ARM_REG_R5) == 0x12345678
    assert em.enter(0x08006E18) == b"\x40\x80"

    em.seed(0x0F, 2)
    em.cpu.reg_write(UC_ARM_REG_R0, 1)
    wire = em.enter(0x08006D50)
    clear = b"\x40\xc0" + bytes(16) + b"\x40\x8a"
    assert wire.startswith(clear)
    redraw = wire[len(clear):]
    assert len(redraw) and len(redraw) % 12 == 0
    for offset in range(0, len(redraw), 12):
        frame = redraw[offset:offset + 12]
        assert frame[:2] == b"\x40\xc0" and frame[-2:] == b"\x40\x8a"

    em.enter(0x0800B2F4, 0x0800B2F8, stack=OLD_SP)
    assert em.cpu.mem_read(STATE, 1) == b"\x00"
    em.enter(0x0800AF2C)  # Run the unmodified stock C data initializer afterward.
    assert em.cpu.mem_read(STATE, 1) == b"\x00"
    em.cpu.mem_write(GLOBAL + 4, b"\x01")
    em.enter(REDRAW)
    assert em.cpu.mem_read(STATE, 3) == b"\xd5\x0f\x07"
    print(
        "Passed 1,536 redraw cases, 128 masked updates, isolated display frames, "
        "stock guards, wake and retained reset."
    )


class ReactiveRendererEmulator(DisplayEmulator):
    def _hook(self, cpu: Uc, address: int, size: int, data: object) -> None:
        if address == 0x0800502A:  # The RGB buffer has been filled and is ready for DMA.
            cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))
        else:
            super()._hook(cpu, address, size, data)


def exercise_reactive(stock: bytes) -> None:
    em = ReactiveRendererEmulator(stock)
    data = build_creative_animation("spectrum_meter").data
    # The MCU's transfer parser writes incoming big-endian pairs as native halfwords.
    stored = bytearray(len(data))
    stored[0::2] = data[1::2]
    stored[1::2] = data[0::2]
    em.cpu.mem_write(0x0800C000, bytes(stored))
    em.seed(15, 7, 100)
    levels = (20, 55, 145, 320, 500, 800, 1227, 1699, 2737, 60_000)
    for band in range(10):
        em.cpu.mem_write(0x200016AC, bytes(10))
        for level, sample in enumerate(levels):
            values = [0] * 10
            values[band] = sample
            em.cpu.mem_write(GLOBAL + 0x40, pack("<10H", *values))
            em.cpu.mem_write(GLOBAL + 0x88, b"\xFF\x00\x01")
            em.enter(0x08006074)
            offset = (band * 10 + level) * FRAME_SIZE + 2
            expected_grb = bytes(
                data[offset + pixel * 4 + channel]
                for pixel in range(LIGHT_COUNT)
                for channel in (1, 0, 2)
            )
            assert em.cpu.mem_read(0x2000162C, LIGHT_COUNT * 3) == expected_grb, (band, level)
    print("Passed 100 Spectrum Meter band/level cases in the stock type-2 renderer.")


def exercise_volume_feedback(stock: bytes) -> None:
    em = ReactiveRendererEmulator(patch_image(stock))
    em.seed(15, 4, 74)
    em.cpu.mem_write(GLOBAL + 0x13, b"\x05")  # Static effect, internal zero-based ID.
    em.cpu.mem_write(GLOBAL + 0x1A, b"\x04")
    em.cpu.mem_write(GLOBAL + 0x32, b"\xff\x6a\x20")
    em.enter(0x080066CC)
    rgb_buffer = 0x2000162C
    original = bytes(em.cpu.mem_read(rgb_buffer, LIGHT_COUNT * 3))
    assert all(original)

    em.cpu.mem_write(COMMAND, b"+VOL+005\x00")
    em.cpu.reg_write(UC_ARM_REG_R4, COMMAND)
    em.enter(0x08002DFE, PARSER_TAIL)
    assert em.cpu.mem_read(GLOBAL + 0x1A, 1) == b"\x05"
    assert em.cpu.mem_read(GLOBAL + 0x84, 2) == b"\xc8\x00"
    em.enter(0x080066CC)
    overlay = bytes(em.cpu.mem_read(rgb_buffer, LIGHT_COUNT * 3))
    assert sum(any(overlay[offset:offset + 3]) for offset in range(0, len(overlay), 3)) == 6

    em.cpu.mem_write(GLOBAL + 0x84, b"\x00\x00")
    em.enter(0x080066CC)
    assert em.cpu.mem_read(rgb_buffer, LIGHT_COUNT * 3) == original
    print("Reproduced the RGB volume-bar overlay and return to static lighting.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock", type=Path, help="exact official 1.3.0 image")
    stock = parser.parse_args().stock.read_bytes()
    exercise(stock)
    exercise_reactive(stock)
    exercise_volume_feedback(stock)


if __name__ == "__main__":
    main()
