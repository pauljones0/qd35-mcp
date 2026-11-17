import asyncio
from dataclasses import replace

import pytest

from qd35_mcp import constants as c
from qd35_mcp.config import Config
from qd35_mcp.errors import DangerousWritesDisabled, DeviceNotFound
from qd35_mcp.service import QD35Service
from qd35_mcp.transport.fake import FakeTransport


@pytest.fixture
def config() -> Config:
    return Config(
        device=None,
        adapter=None,
        transport="fake",
        writes_enabled=True,
        dangerous_writes_enabled=False,
        request_timeout=1,
        scan_timeout=1,
    )


@pytest.fixture
def service(config: Config) -> QD35Service:
    return QD35Service(FakeTransport(), config)


async def test_status_reports_complete_simulated_state(service: QD35Service) -> None:
    status = await service.status()
    assert status["volume"] == {"maximum": 16, "current": 6}
    assert status["input"]["name"] == "bluetooth"
    assert status["lighting"]["selected_effect"] == "breathing"


async def test_semantic_changes_verify_readback(service: QD35Service) -> None:
    assert (await service.set_volume(9))["applied"] is True
    assert (await service.set_input("usb"))["applied"] is True
    assert (await service.set_eq_preset("movie"))["applied"] is True
    eq = await service.set_eq_band(2, 1.5)
    assert eq["applied"] is True
    assert eq["active_preset"]["name"] == "diy"
    light = await service.set_light("static", "#FF8800", 40)
    assert light["applied"] is True


class DelayedInputTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.pending_input: tuple[int, int] | None = None
        self.input_queries_after_set = 0

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.SET_INPUT:
            self.pending_input = (payload[0], payload[1])
            self.input_queries_after_set = 0
            return b"\x01"
        if opcode == c.QUERY_INPUT and self.pending_input is not None:
            self.input_queries_after_set += 1
            if self.input_queries_after_set == 3:
                self.input_group, self.input_value = self.pending_input
                self.pending_input = None
        return await super().request(opcode, payload, timeout)


async def test_input_change_waits_for_delayed_firmware_readback(config: Config) -> None:
    transport = DelayedInputTransport()
    service = QD35Service(transport, config)

    result = await service.set_input("aux")

    assert result == {
        "applied": True,
        "input": {"group": 2, "value": 2, "name": "aux"},
    }
    assert transport.input_queries_after_set == 3


async def test_dangerous_operations_are_separately_gated(config: Config) -> None:
    service = QD35Service(FakeTransport(), config)
    with pytest.raises(DangerousWritesDisabled):
        await service.factory_reset()
    enabled = QD35Service(FakeTransport(), replace(config, dangerous_writes_enabled=True))
    assert (await enabled.factory_reset())["sent"] is True


async def test_media_control_uses_one_way_command(config: Config) -> None:
    transport = FakeTransport()
    service = QD35Service(transport, config)

    result = await service.media_control("pause")

    assert result["sent"] is True
    assert result["acknowledgement"] is None
    assert transport.last_media_action == c.MEDIA_ACTIONS["pause"]


async def test_rgb_rejects_empty_and_non_hex_values(service: QD35Service) -> None:
    for value in ("", "12 3456", "GG0000", "#12345"):
        with pytest.raises(ValueError, match="six-digit hex"):
            await service.set_light(rgb=value)


class IgnoredLightFieldsTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.SET_LIGHT:
            record = self.light_records[payload[1]]
            self.light_selected = payload[1]
            record[4] = payload[5]
            return payload
        return await super().request(opcode, payload, timeout)


async def test_light_requires_all_requested_fields_in_readback(config: Config) -> None:
    service = QD35Service(IgnoredLightFieldsTransport(), config)
    result = await service.set_light("breathing", "#123456", 30, 4)
    assert result["applied"] is False


class YieldingFakeTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        await asyncio.sleep(0)
        return await super().request(opcode, payload, timeout)


async def test_partial_light_transactions_are_serialized(config: Config) -> None:
    service = QD35Service(YieldingFakeTransport(), config)
    rgb_result, brightness_result = await asyncio.gather(
        service.set_light(rgb="#123456"),
        service.set_light(brightness=20),
    )
    assert rgb_result["applied"] is True
    assert brightness_result["applied"] is True
    status = await service.status()
    selected = next(
        item
        for item in status["lighting"]["effects"]
        if item["id"] == status["lighting"]["selected_effect_id"]
    )
    assert selected["rgb"] == "#123456"
    assert selected["brightness"] == 20


class AckThenDisappearTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.restarting = False

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.SET_LDAC:
            self.ldac = payload[0]
            self.restarting = True
            return b"\x01"
        if opcode == c.QUERY_LDAC and self.restarting:
            self._connected = False
            raise DeviceNotFound("radio restarting")
        return await super().request(opcode, payload, timeout)


async def test_ldac_ack_then_disconnect_returns_recovery(config: Config) -> None:
    service = QD35Service(AckThenDisappearTransport(), config)
    result = await service.set_ldac("48khz")
    assert result["applied"] is None
    assert result["bluetooth_restarted"] is True
    assert "pair again" in result["suggested_action"]


async def test_simulated_reset_and_power_off_have_real_state_effects(config: Config) -> None:
    enabled = replace(config, dangerous_writes_enabled=True)
    transport = FakeTransport()
    service = QD35Service(transport, enabled)
    await service.set_volume(12)
    await service.set_light(rgb="#123456", brightness=20)
    await service.factory_reset()
    status = await service.status()
    assert status["volume"]["current"] == 6
    selected = next(
        item
        for item in status["lighting"]["effects"]
        if item["id"] == status["lighting"]["selected_effect_id"]
    )
    assert selected["rgb"] == "#E6E6FF"
    assert selected["brightness"] == 100

    await service.power_off()
    assert (await service.discover())["count"] == 0
    with pytest.raises(DeviceNotFound, match="powered off"):
        await service.status()
