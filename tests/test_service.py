import asyncio
from dataclasses import replace
from datetime import UTC
from datetime import datetime as RealDatetime

import pytest

from qd35_mcp import constants as c
from qd35_mcp import service as service_module
from qd35_mcp.config import Config
from qd35_mcp.custom_lights import CUSTOM_LIGHTS
from qd35_mcp.errors import (
    DangerousWritesDisabled,
    DeviceNotFound,
    OutcomeUnknown,
    ProtocolError,
)
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


class DropDuringStatusTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.connect_calls = 0

    async def connect(self, device: str | None = None):
        self.connect_calls += 1
        return await super().connect(device)

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        result = await super().request(opcode, payload, timeout)
        if opcode == c.QUERY_CAPABILITIES:
            self._connected = False
        return result


async def test_status_never_reconnects_mid_snapshot(config: Config) -> None:
    transport = DropDuringStatusTransport()
    status = await QD35Service(transport, config).status()
    assert status["device"]["address"] == transport.device.address
    assert status["volume"]["unavailable"] is True
    assert status["name"]["unavailable"] is True
    assert transport.connect_calls == 1


async def test_lighting_events_use_opcode_specific_layouts(config: Config) -> None:
    transport = FakeTransport()
    transport._emit(c.SET_LIGHT, bytes.fromhex("010604050632ff"))
    full_state = await transport.request(c.QUERY_LIGHT)
    transport._emit(c.RESET_LIGHT, full_state)
    events = await QD35Service(transport, config).events()
    assert events["events"][0]["parsed"]["effect"] == "static"
    assert events["events"][1]["parsed"]["selected_effect"] == "breathing"
    assert all("parse_error" not in event for event in events["events"])


async def test_semantic_changes_verify_readback(service: QD35Service) -> None:
    assert (await service.set_volume(9))["applied"] is True
    assert (await service.set_input("usb"))["applied"] is True
    assert (await service.set_eq_preset("movie"))["applied"] is True
    eq = await service.set_eq_band(2, 1.5)
    assert eq["applied"] is True
    assert eq["active_preset"]["name"] == "diy"
    light = await service.set_light("static", "#FF8800", brightness=40)
    assert light["applied"] is True
    assert (await service.reset_eq())["applied"] is True


async def test_custom_light_upload_uses_negotiated_geometry(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = CUSTOM_LIGHTS["gradient"]
    animation = bytes((0, 20)) + bytes((255, 0, 0, 0)) * 37
    monkeypatch.setattr(
        service_module,
        "fetch_custom_light_asset",
        lambda _preset, _palette: (asset, animation),
    )
    transport = FakeTransport()
    result = await QD35Service(transport, config).apply_custom_light("gradient")
    assert result["applied"] is True
    assert result["chunks_uploaded"] == 1
    assert result["bytes_uploaded"] == 150
    assert result["activated"] is True
    assert transport.custom_light_status == (23, 0)
    assert result["activated"] is True
    extended = transport.light_records[7]
    assert extended[1:4] == [1, 0, 0]
    assert extended[4] > 0


async def test_diagnostic_light_upload_is_bounded_and_uses_low_brightness(
    config: Config,
) -> None:
    transport = FakeTransport()
    result = await QD35Service(transport, config).apply_custom_light(
        "rgb_bars", activation_brightness=10
    )
    assert result["applied"] is True
    assert result["generated_animation"]["frames"] == 1
    assert result["bytes_uploaded"] == 150
    assert result["custom_light_after"]["asset_id"] == 1
    assert result["lighting"]["effects"][-1]["brightness"] == 10


async def test_fake_custom_light_preserves_palette(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = CUSTOM_LIGHTS["spiral"]
    animation = bytes((0, 100)) + bytes((255, 255, 255, 0)) * 37
    monkeypatch.setattr(
        service_module,
        "fetch_custom_light_asset",
        lambda _preset, _palette: (asset, animation),
    )
    result = await QD35Service(FakeTransport(), config).apply_custom_light(
        "spiral", palette_key=1
    )
    assert result["applied"] is True
    assert result["custom_light_after"]["palette_key"] == 1


class DropAfterEqWriteTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        result = await super().request(opcode, payload, timeout)
        if opcode == c.SET_EQ:
            self._connected = False
        return result


async def test_transaction_never_reconnects_between_mutation_steps(config: Config) -> None:
    transport = DropAfterEqWriteTransport()
    result = await QD35Service(transport, config).set_eq_band(0, 1.0)
    assert result["applied"] is None
    assert result["completed_steps"] == ["band_write"]
    assert "refusing to reconnect" in result["error"]


class SpoofedNameTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.name = "NOT A QD35"
        self.maximum = 99


async def test_qd35_name_alone_cannot_bypass_fingerprint(config: Config) -> None:
    transport = SpoofedNameTransport()
    with pytest.raises(ProtocolError, match="could not be verified"):
        await QD35Service(transport, config).set_volume(5)
    assert transport.volume == 6


class MalformedCustomStartTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.abort_calls = 0

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.START_CUSTOM_LIGHT_TRANSFER:
            await super().request(opcode, payload, timeout)
            return b"\x00"
        if opcode == c.FINISH_CUSTOM_LIGHT_TRANSFER and payload == b"\x02":
            self.abort_calls += 1
        return await super().request(opcode, payload, timeout)


async def test_malformed_custom_start_is_aborted(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = CUSTOM_LIGHTS["gradient"]
    animation = bytes((0, 20)) + bytes((255, 0, 0, 0)) * 37
    monkeypatch.setattr(
        service_module,
        "fetch_custom_light_asset",
        lambda _preset, _palette: (asset, animation),
    )
    transport = MalformedCustomStartTransport()
    result = await QD35Service(transport, config).apply_custom_light("gradient")
    assert result["applied"] is None
    assert result["abort_acknowledged"] is True
    assert transport.abort_calls == 1
    assert transport.custom_light_transfer is None


class RetryCustomChunkTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.chunk_attempts: dict[int, int] = {}

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.PROCESS_CUSTOM_LIGHT_TRANSFER:
            sequence = payload[2]
            self.chunk_attempts[sequence] = self.chunk_attempts.get(sequence, 0) + 1
            if self.chunk_attempts[sequence] == 1:
                if sequence == 1:
                    await super().request(opcode, payload, timeout)
                    raise service_module.RequestTimeout("injected lost acknowledgement")
                return bytes((0, sequence - 2))
        return await super().request(opcode, payload, timeout)


async def test_custom_chunks_retry_timeout_and_reject_stale_sequence_ack(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = CUSTOM_LIGHTS["gradient"]
    record = bytes((0, 20)) + bytes((255, 0, 0, 0)) * 37
    monkeypatch.setattr(
        service_module,
        "fetch_custom_light_asset",
        lambda _preset, _palette: (asset, record * 2),
    )
    transport = RetryCustomChunkTransport()
    result = await QD35Service(transport, config).apply_custom_light("gradient")
    assert result["applied"] is True
    assert transport.chunk_attempts == {1: 2, 2: 2}
    assert result["chunk_ack_details"] == [0, 1]
    assert result["all_chunk_acks_sequence_correlated"] is True
    assert transport.custom_light_bytes_written == 300


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
        if result.get("bluetooth_restarted") is True:
            assert result["changed"] is None
            assert result["mutation_sent"] is True
        else:
            assert result["changed"] is False
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


class UnsupportedCapabilityTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.QUERY_CAPABILITIES:
            return bytes(22)
        return await super().request(opcode, payload, timeout)


async def test_writes_are_gated_by_live_capabilities(config: Config) -> None:
    service = QD35Service(UnsupportedCapabilityTransport(), config)
    with pytest.raises(ProtocolError, match="LDAC"):
        await service.set_ldac("48khz")
    dangerous = QD35Service(
        UnsupportedCapabilityTransport(),
        replace(config, dangerous_writes_enabled=True),
    )
    with pytest.raises(ProtocolError, match="factory_reset"):
        await dangerous.factory_reset()


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
        ("12h", bytes((13, 4, 5, 1, 1))),
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


class PackagedRenameTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.QUERY_CAPABILITIES:
            capabilities = bytearray(await super().request(opcode, payload, timeout))
            capabilities[3] = 1
            return bytes(capabilities)
        return await super().request(opcode, payload, timeout)


async def test_packaged_rename_mode_is_rejected_until_verified(config: Config) -> None:
    transport = PackagedRenameTransport()
    with pytest.raises(ProtocolError, match="packaged writes"):
        await QD35Service(transport, config).rename("test")
    assert transport.name == "EDIFIER QD35 (simulated)"


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


class ConnectedUnknownDisruptiveTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.FACTORY_RESET:
            raise OutcomeUnknown("confirmation timed out while the link remained up")
        return await super().request(opcode, payload, timeout)


async def test_disruptive_unknown_does_not_invent_a_disconnect(config: Config) -> None:
    service = QD35Service(
        ConnectedUnknownDisruptiveTransport(),
        replace(config, dangerous_writes_enabled=True),
    )
    result = await service.factory_reset()
    assert result["sent"] is True
    assert result["connection_dropped"] is False
    assert "remained up" in result["error"]


class ShortAddressTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.QUERY_ADDRESS:
            return b"\x01\x02\x03"
        return await super().request(opcode, payload, timeout)


async def test_status_marks_malformed_address_unavailable(config: Config) -> None:
    status = await QD35Service(ShortAddressTransport(), config).status()
    assert status["classic_address"]["unavailable"] is True


class EmptyEqResetTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.reset_done = False

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.RESET_EQ:
            self.reset_done = True
        if opcode == c.QUERY_EQ and self.reset_done:
            return b"\x03\x00"
        return await super().request(opcode, payload, timeout)


async def test_eq_reset_rejects_empty_band_readback(config: Config) -> None:
    result = await QD35Service(EmptyEqResetTransport(), config).reset_eq()
    assert result["applied"] is False


class WrongFirmwareCustomTransport(FakeTransport):
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.QUERY_FIRMWARE:
            return b"\x09\x09\x09"
        return await super().request(opcode, payload, timeout)


async def test_custom_assets_fail_closed_on_unverified_firmware(config: Config) -> None:
    transport = WrongFirmwareCustomTransport()
    with pytest.raises(ProtocolError, match="only for QD35 firmware 1.3.0"):
        await QD35Service(transport, config).apply_custom_light("gradient")
    assert transport.custom_light_transfer is None


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


async def test_unknown_selected_light_is_preserved_but_not_blindly_written(
    config: Config,
) -> None:
    transport = FakeTransport()
    transport.light_records[8] = [8, 1, 2, 3, 100, 255]
    transport.light_selected = 8
    service = QD35Service(transport, config)
    status = await service.status()
    assert status["lighting"]["selected_effect"] == "effect_8"
    with pytest.raises(ProtocolError, match="unknown lighting effect 8"):
        await service.set_light(brightness=20)


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


async def test_eq_missing_requested_band_reports_partial_mutation(config: Config) -> None:
    result = await QD35Service(MissingEqReadbackTransport(), config).set_eq_band(5, 1.0)
    assert result["applied"] is None
    assert result["outcome"] == "unknown_after_write"
    assert result["completed_steps"] == ["band_write", "diy_preset_write"]
    assert "omitted band 5" in result["error"]


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


async def test_light_missing_requested_effect_reports_unknown_outcome(config: Config) -> None:
    result = await QD35Service(MissingLightReadbackTransport(), config).set_light("static")
    assert result["applied"] is None
    assert result["completed_steps"] == ["lighting_write"]
    assert "omitted requested effect 6" in result["error"]


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


class PausedVolumeTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.write_started = asyncio.Event()
        self.release_write = asyncio.Event()

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        result = await super().request(opcode, payload, timeout)
        if opcode == c.SET_VOLUME:
            self.write_started.set()
            await self.release_write.wait()
        return result


async def test_disconnect_waits_for_complete_semantic_transaction(config: Config) -> None:
    transport = PausedVolumeTransport()
    service = QD35Service(transport, config)
    mutation = asyncio.create_task(service.set_volume(9))
    await transport.write_started.wait()
    disconnect = asyncio.create_task(service.disconnect())
    await asyncio.sleep(0)
    assert disconnect.done() is False
    transport.release_write.set()
    assert (await mutation)["applied"] is True
    assert await disconnect == {"connected": False}
    assert transport.connected is False


class LostCustomReadbackTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.finished = False

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        result = await super().request(opcode, payload, timeout)
        if opcode == c.FINISH_CUSTOM_LIGHT_TRANSFER and payload == b"\x01":
            self.finished = True
        if opcode == c.QUERY_CUSTOM_LIGHT and self.finished:
            raise DeviceNotFound("link lost before custom-light readback")
        return result


async def test_custom_light_reports_completed_transfer_with_failed_readback(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = CUSTOM_LIGHTS["gradient"]
    animation = bytes((0, 20)) + bytes((255, 0, 0, 0)) * 37
    monkeypatch.setattr(
        service_module,
        "fetch_custom_light_asset",
        lambda _preset, _palette: (asset, animation),
    )
    result = await QD35Service(LostCustomReadbackTransport(), config).apply_custom_light(
        "gradient"
    )
    assert result["applied"] is None
    assert result["completion_acknowledged"] is True
    assert result["outcome"] == "transfer_completed_but_readback_failed"
    assert result["retry_automatically"] is False


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
            raise DeviceNotFound("radio restarting", connection_dropped=True)
        return await super().request(opcode, payload, timeout)


async def test_ldac_ack_then_disconnect_returns_recovery(config: Config) -> None:
    service = QD35Service(AckThenDisappearTransport(), config)
    result = await service.set_ldac("48khz")
    assert result["applied"] is None
    assert result["bluetooth_restarted"] is True
    assert "pair again" in result["suggested_action"]


class MissingLdacReadbackTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.changed = False

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.SET_LDAC:
            self.ldac = payload[0]
            self.changed = True
            return b"\x01"
        if opcode == c.QUERY_LDAC and self.changed:
            raise service_module.RequestTimeout(
                "readback reply missing", connection_dropped=False
            )
        return await super().request(opcode, payload, timeout)


async def test_ldac_missing_readback_does_not_invent_restart(config: Config) -> None:
    result = await QD35Service(MissingLdacReadbackTransport(), config).set_ldac("48khz")
    assert result["mutation_sent"] is True
    assert result["connection_dropped"] is False
    assert result["bluetooth_restarted"] is None


class MismatchedCustomIdentityTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.finished = False
        self.light_writes = 0

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if opcode == c.SET_LIGHT:
            self.light_writes += 1
        result = await super().request(opcode, payload, timeout)
        if opcode == c.FINISH_CUSTOM_LIGHT_TRANSFER and payload == b"\x01":
            self.finished = True
        if opcode == c.QUERY_CUSTOM_LIGHT and self.finished:
            return b"\x7f\x00"
        return result


async def test_custom_identity_mismatch_is_not_activated(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = CUSTOM_LIGHTS["gradient"]
    animation = bytes((0, 20)) + bytes((255, 0, 0, 0)) * 37
    monkeypatch.setattr(
        service_module,
        "fetch_custom_light_asset",
        lambda _preset, _palette: (asset, animation),
    )
    transport = MismatchedCustomIdentityTransport()
    result = await QD35Service(transport, config).apply_custom_light("gradient")
    assert result["applied"] is False
    assert result["activated"] is False
    assert transport.light_writes == 0


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
