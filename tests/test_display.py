from __future__ import annotations

import pytest

from qd35_mcp.config import Config
from qd35_mcp.display import (
    DISPLAY_CLOCK,
    DISPLAY_DEFAULT_FLAGS,
    DISPLAY_FOLLOW_DIAL,
    DISPLAY_LOGO,
    DISPLAY_POWER,
    DISPLAY_RAW_MODE,
    DISPLAY_SYMBOLS,
    describe_flags,
    grid_payload,
    state_payload,
)
from qd35_mcp.errors import DangerousWritesDisabled, ProtocolError
from qd35_mcp.service import QD35Service
from qd35_mcp.transport.fake import FakeTransport


def config(*, custom: bool = True, dangerous: bool = True) -> Config:
    return Config(
        device="fake",
        adapter=None,
        transport="fake",
        writes_enabled=True,
        dangerous_writes_enabled=dangerous,
        request_timeout=1,
        scan_timeout=1,
        custom_display_firmware=custom,
    )


def test_payload_validation() -> None:
    assert state_payload(0x3F, 7) == bytes.fromhex("0100d03f070035")
    assert grid_payload(3, 0xA5) == bytes.fromhex("0100d103a50035")
    with pytest.raises(ValueError):
        state_payload(0x40, 7)
    with pytest.raises(ValueError):
        grid_payload(8, 0)


def test_display_state_reports_physical_logo_coupling() -> None:
    clock_only = describe_flags(DISPLAY_CLOCK | DISPLAY_POWER, 0)
    assert clock_only["logo_grid_enabled"] is False
    assert clock_only["visible_logo_extinction"] == "not_achievable_with_clock_on"
    powered_off = describe_flags(0, 0)
    assert powered_off["visible_logo_extinction"] == "verified"


@pytest.mark.asyncio
async def test_semantic_display_controls_are_independent() -> None:
    transport = FakeTransport()
    service = QD35Service(transport, config())
    result = await service.set_display(
        clock=False,
        logo=True,
        symbols=False,
        brightness=2,
        follow_lighting_dial=True,
    )
    assert result["acknowledged"] is True
    assert result["applied"] is None
    assert transport.display_flags & DISPLAY_CLOCK == 0
    assert transport.display_flags & DISPLAY_LOGO
    assert transport.display_flags & DISPLAY_SYMBOLS == 0
    assert transport.display_flags & DISPLAY_FOLLOW_DIAL
    assert transport.display_brightness == 2
    assert transport.light_selected == 4


@pytest.mark.asyncio
async def test_raw_grid_and_reset() -> None:
    transport = FakeTransport()
    service = QD35Service(transport, config())
    await service.set_display(raw_mode=True)
    assert transport.display_flags & DISPLAY_RAW_MODE
    result = await service.write_display_grid_byte(6, 0xA5)
    assert result["value_hex"] == "0xA5"
    assert transport.display_grid[6] == 0xA5
    reset = await service.reset_display()
    assert reset["state"]["raw_mode"] is False
    assert transport.display_flags == DISPLAY_DEFAULT_FLAGS
    assert transport.display_grid == [0xFF] * 8


@pytest.mark.asyncio
async def test_custom_firmware_gate_fails_closed() -> None:
    service = QD35Service(FakeTransport(), config(custom=False))
    with pytest.raises(ProtocolError, match="MCU patch"):
        await service.set_display(clock=False)


@pytest.mark.asyncio
async def test_raw_grid_requires_dangerous_gate() -> None:
    service = QD35Service(FakeTransport(), config(dangerous=False))
    with pytest.raises(DangerousWritesDisabled):
        await service.write_display_grid_byte(0, 0)


@pytest.mark.asyncio
async def test_firmware_install_requires_separate_gate() -> None:
    service = QD35Service(FakeTransport(), config())
    with pytest.raises(ProtocolError, match="QD35_ENABLE_FIRMWARE_INSTALL"):
        await service.install_display_firmware(b"not firmware")
