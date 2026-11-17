import asyncio
from dataclasses import replace
from datetime import UTC
from datetime import datetime as RealDatetime

import pytest

from qd35_mcp import constants as c
from qd35_mcp import service as service_module
from qd35_mcp.config import Config
from qd35_mcp.errors import DangerousWritesDisabled, DeviceNotFound, ProtocolError
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
    light = await service.set_light("static", "#FF8800", brightness=40)
    assert light["applied"] is True


async def test_exhaustive_semantic_matrix(config: Config) -> None:
    transport = FakeTransport()
    service = QD35Service(transport, config)

    for source in c.INPUTS:
        result = await service.set_input(source)
        assert result["applied"] is True
        assert result["input"]["name"] == source

    for preset in c.EQ_PRESETS:
        result = await service.set_eq_preset(preset)
        assert result["applied"] is True
        assert result["eq_preset"]["name"] == preset

    for band in range(6):
        for gain_db in (-3.0, 0.0, 3.0):
            result = await service.set_eq_band(band, gain_db)
            assert result["applied"] is True
            assert result["band"]["gain_db"] == gain_db

    for effect, effect_id in c.LIGHT_EFFECTS.items():
        if effect_id in c.LIGHT_RGB_EFFECT_IDS:
            result = await service.set_light(effect, "#123456", brightness=40)
        else:
            mode = "deep" if effect_id in (1, 2) else "on"
            result = await service.set_light(effect, color_mode=mode, brightness=40)
        assert result["applied"] is True
    for effect in ("ambient", "magic_tides", "breathing"):
        for speed in (1, 10):
            result = await service.set_light(effect=effect, speed=speed)
            assert result["applied"] is True

    assert (await service.set_light(enabled=False))["applied"] is True
    status = await service.status()
    selected = next(
        item
        for item in status["lighting"]["effects"]
        if item["id"] == status["lighting"]["selected_effect_id"]
    )
    assert selected["brightness"] == 0
    assert (await service.set_light(enabled=True))["applied"] is True

    for action, value in c.MEDIA_ACTIONS.items():
        assert (await service.media_control(action))["sent"] is True
        assert transport.last_media_action == value

    for mode, value in c.LDAC_MODES.items():
        result = await service.set_ldac(mode)
        assert result["changed"] is (
            transport.ldac == value and result.get("bluetooth_restarted") is True
        )
        assert transport.ldac == value


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
    assert result["applied"] is None
    assert result["changed"] is False
    assert result["playback_after"] == {"value": 3, "playing": False}
    assert transport.last_media_action == c.MEDIA_ACTIONS["pause"]

    play_result = await service.media_control("play")
    assert play_result["applied"] is True
    assert play_result["changed"] is True


async def test_light_rejects_contradictory_enabled_and_brightness(
    service: QD35Service,
) -> None:
    with pytest.raises(ValueError, match="enabled=true"):
        await service.set_light(enabled=True, brightness=0)
    with pytest.raises(ValueError, match="enabled=false"):
        await service.set_light(enabled=False, brightness=20)


class FixedDatetime:
    @classmethod
    def now(cls) -> RealDatetime:
        return RealDatetime(2026, 11, 17, 13, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("clock_format", "expected"),
    [
        ("preserve", bytes((13, 4, 5, 0))),
        ("12h", bytes((1, 4, 5, 1, 1))),
        ("24h", bytes((13, 4, 5, 2, 1))),
    ],
)
async def test_clock_payload_dialects(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    clock_format: str,
    expected: bytes,
) -> None:
    monkeypatch.setattr(service_module, "datetime", FixedDatetime)
    transport = FakeTransport()
    service = QD35Service(transport, config)

    result = await service.sync_clock(clock_format)

    assert result["response"] == "01"
    assert result["acknowledged"] is True
    assert result["applied"] is None
    assert result["attempts"] == 1
    assert transport.last_clock_payload == expected


class ResyncClockTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.clock_calls = 0

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.SYNC_CLOCK:
            self.clock_calls += 1
            self.last_clock_payload = payload
            return b"\x02" if self.clock_calls == 1 else b"\x01"
        return await super().request(opcode, payload, timeout)


async def test_clock_retries_one_resynchronization_request(config: Config) -> None:
    transport = ResyncClockTransport()
    result = await QD35Service(transport, config).sync_clock("24h")
    assert result["acknowledged"] is True
    assert result["applied"] is None
    assert result["attempts"] == 2
    assert transport.clock_calls == 2


async def test_rename_rejects_control_characters(service: QD35Service) -> None:
    with pytest.raises(ValueError, match="control characters"):
        await service.rename("living\nroom")


class FailedDisruptiveAckTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.FACTORY_RESET:
            return b"\x02"
        return await super().request(opcode, payload, timeout)


async def test_disruptive_command_requires_success_ack(config: Config) -> None:
    service = QD35Service(
        FailedDisruptiveAckTransport(), replace(config, dangerous_writes_enabled=True)
    )
    with pytest.raises(ProtocolError, match="unexpected response 02"):
        await service.factory_reset()


class ShortAddressTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.QUERY_ADDRESS:
            return b"\x01\x02\x03"
        return await super().request(opcode, payload, timeout)


async def test_status_marks_malformed_address_unavailable(config: Config) -> None:
    status = await QD35Service(ShortAddressTransport(), config).status()
    assert status["classic_address"]["unavailable"] is True


async def test_discover_rejects_nonpositive_timeout(service: QD35Service) -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        await service.discover(0)


async def test_rgb_rejects_empty_and_non_hex_values(service: QD35Service) -> None:
    for value in ("", "12 3456", "GG0000", "#12345"):
        with pytest.raises(ValueError, match="six-digit hex"):
            await service.set_light(rgb=value)


async def test_lighting_enforces_effect_specific_color_controls(service: QD35Service) -> None:
    with pytest.raises(ValueError, match="uses color_mode"):
        await service.set_light("ambient", rgb="#123456")
    with pytest.raises(ValueError, match="uses rgb"):
        await service.set_light("static", color_mode="deep")
    with pytest.raises(ValueError, match="cannot be used together"):
        await service.set_light("static", rgb="#123456", color_mode="deep")
    with pytest.raises(ValueError, match="must be one of"):
        await service.set_light("extended", color_mode="deep")


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
    result = await service.set_light("breathing", "#123456", brightness=30, speed=4)
    assert result["applied"] is False


class MissingEqReadbackTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.wrote_eq = False

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.SET_EQ:
            self.wrote_eq = True
        if opcode == c.QUERY_EQ and self.wrote_eq:
            return bytes((3, 1)) + bytes(self.bands[0])
        return await super().request(opcode, payload, timeout)


async def test_eq_missing_requested_band_is_protocol_error(config: Config) -> None:
    with pytest.raises(ProtocolError, match="omitted band 5"):
        await QD35Service(MissingEqReadbackTransport(), config).set_eq_band(5, 1.0)


class MissingLightReadbackTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.wrote_light = False

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.SET_LIGHT:
            self.wrote_light = True
            return b"\x01"
        if opcode == c.QUERY_LIGHT and self.wrote_light:
            records = b"".join(
                bytes(record) for effect_id, record in self.light_records.items() if effect_id != 6
            )
            return bytes((self.light_group, self.light_selected)) + records
        return await super().request(opcode, payload, timeout)


async def test_light_missing_requested_effect_is_protocol_error(config: Config) -> None:
    with pytest.raises(ProtocolError, match="omitted requested effect 6"):
        await QD35Service(MissingLightReadbackTransport(), config).set_light("static")


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
    assert transport.audio_connected is False
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
