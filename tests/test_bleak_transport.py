import asyncio
from types import SimpleNamespace

import bleak
import pytest

from qd35_mcp import constants as c
from qd35_mcp.errors import OutcomeUnknown, ProtocolError, RequestTimeout, TransportError
from qd35_mcp.transport import bleak as bleak_module
from qd35_mcp.transport.base import DeviceCandidate
from qd35_mcp.transport.bleak import BleakTransport


def reply(opcode: int, payload: bytes) -> bytearray:
    data = bytearray((0xBB, 0xEC, opcode, len(payload) >> 8, len(payload) & 0xFF))
    data.extend(payload)
    data.append(sum(data) & 0xFF)
    return data


class MockClient:
    instances: list["MockClient"] = []
    fail_notify = False
    fail_write = False
    auto_reply = False
    ready_event: asyncio.Event

    def __init__(self, _device: object, **kwargs: object) -> None:
        self.is_connected = False
        self.disconnected_callback = kwargs["disconnected_callback"]
        self.notification_callback = None
        self.writes: list[bytes] = []
        self.__class__.instances.append(self)

    async def connect(self) -> None:
        await asyncio.sleep(0)
        self.is_connected = True

    async def start_notify(self, _uuid: str, callback: object) -> None:
        if self.fail_notify:
            raise OSError("notification subscription denied")
        self.notification_callback = callback
        self.ready_event.set()

    async def stop_notify(self, _uuid: str) -> None:
        return None

    async def disconnect(self) -> None:
        self.is_connected = False
        self.disconnected_callback(self)

    async def write_gatt_char(self, _uuid: str, packet: bytes, response: bool = False) -> None:
        if self.fail_write:
            raise OSError("injected write failure")
        self.writes.append(packet)
        if self.auto_reply and self.notification_callback:
            self.notification_callback(None, reply(packet[2], b"\x10\x06"))


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> BleakTransport:
    MockClient.instances = []
    MockClient.fail_notify = False
    MockClient.fail_write = False
    MockClient.auto_reply = False
    MockClient.ready_event = asyncio.Event()
    monkeypatch.setattr(bleak, "BleakClient", MockClient)
    result = BleakTransport(scan_timeout=0.01)
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:01", name=None)
    candidate = DeviceCandidate(
        address=device.address,
        name=None,
        rssi=-40,
        manufacturer_id=c.EDIFIER_COMPANY_ID,
        service_uuids=(c.SEARCH_UUID,),
        protocol_version=2,
        encryption_selector=0,
    )

    async def scan(_timeout: float) -> list[tuple[object, object, DeviceCandidate]]:
        await asyncio.sleep(0)
        return [(device, object(), candidate)]

    monkeypatch.setattr(result, "_scan", scan)
    return result


async def test_concurrent_cold_connect_publishes_one_ready_client(
    transport: BleakTransport,
) -> None:
    first, second = await asyncio.gather(transport.connect(), transport.connect())
    assert first.address == second.address
    assert len(MockClient.instances) == 1
    assert transport.connected is True
    assert MockClient.instances[0].notification_callback is not None


async def test_notification_setup_failure_cleans_up_session(
    transport: BleakTransport,
) -> None:
    MockClient.fail_notify = True
    with pytest.raises(TransportError, match="notification subscription denied"):
        await transport.connect()
    assert transport.connected is False
    assert MockClient.instances[0].is_connected is False


async def test_obsolete_disconnect_callback_cannot_clear_new_client(
    transport: BleakTransport,
) -> None:
    await transport.connect()
    old = MockClient.instances[0]
    await transport.disconnect()
    await transport.connect()
    new = MockClient.instances[1]
    old.disconnected_callback(old)
    assert transport.connected is True
    assert transport._client is new


async def test_timeout_invalidates_session_before_same_opcode_retry(
    transport: BleakTransport,
) -> None:
    with pytest.raises(RequestTimeout):
        await transport.request(c.QUERY_VOLUME, timeout=0.01)
    assert transport.connected is False
    assert MockClient.instances[0].is_connected is False

    MockClient.auto_reply = True
    assert await transport.request(c.QUERY_VOLUME, timeout=0.1) == b"\x10\x06"
    assert len(MockClient.instances) == 2


async def test_mutating_timeout_is_unknown_even_when_connection_stays_up(
    transport: BleakTransport,
) -> None:
    with pytest.raises(OutcomeUnknown, match="write outcome unknown"):
        await transport.request(c.SET_VOLUME, b"\x08", timeout=0.01)
    assert transport.connected is False


async def test_malformed_mutating_reply_is_unknown(transport: BleakTransport) -> None:
    await transport.connect()
    client = MockClient.instances[0]
    task = asyncio.create_task(transport.request(c.SET_VOLUME, b"\x08", timeout=1))
    await asyncio.sleep(0)
    corrupt = reply(c.SET_VOLUME, b"\x01")
    corrupt[-1] ^= 0xFF
    client.notification_callback(None, corrupt)
    with pytest.raises(OutcomeUnknown, match="invalid confirmation"):
        await task
    assert transport.connected is False


async def test_cancelled_request_invalidates_session(transport: BleakTransport) -> None:
    task = asyncio.create_task(transport.request(c.QUERY_VOLUME, timeout=5))
    await MockClient.ready_event.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.connected is False
    assert MockClient.instances[0].is_connected is False


async def test_one_way_send_does_not_wait_for_notification(
    transport: BleakTransport,
) -> None:
    await transport.send(c.MEDIA_CONTROL, b"\x01")

    assert transport.connected is True
    assert MockClient.instances[0].writes[0][2] == c.MEDIA_CONTROL
    assert MockClient.instances[0].writes[0][5] == 1


async def test_large_frame_uses_negotiated_mtu_despite_stale_characteristic_limit(
    transport: BleakTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    await transport.connect()
    client = MockClient.instances[0]
    characteristic = SimpleNamespace(max_write_without_response_size=20)
    client.services = SimpleNamespace(get_characteristic=lambda _uuid: characteristic)
    client.mtu_size = 23

    class Backend:
        async def _acquire_mtu(self) -> None:
            client.mtu_size = 512

    client._backend = Backend()

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(bleak_module.asyncio, "sleep", no_sleep)
    packet = bytes(range(163))
    await transport._write_packet_locked(packet)
    assert client.mtu_size == 512
    assert client.writes == [packet]
    assert transport._mtu_ready is True


async def test_large_frame_refuses_unvalidated_fragmentation(
    transport: BleakTransport,
) -> None:
    await transport.connect()
    client = MockClient.instances[0]
    client.services = SimpleNamespace(
        get_characteristic=lambda _uuid: SimpleNamespace(max_write_without_response_size=20)
    )
    client.mtu_size = 23

    class Backend:
        async def _acquire_mtu(self) -> None:
            raise OSError("MTU negotiation denied")

    client._backend = Backend()
    with pytest.raises(TransportError, match="MTU negotiation failed"):
        await transport._write_packet_locked(bytes(range(163)))
    assert client.writes == []


async def test_ota_fragmentation_matches_current_edifier_app(
    transport: BleakTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    await transport.connect()
    client = MockClient.instances[0]
    characteristic = SimpleNamespace(max_write_without_response_size=500)
    client.services = SimpleNamespace(get_characteristic=lambda _uuid: characteristic)
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(bleak_module.asyncio, "sleep", record_sleep)
    packet = bytes(index % 256 for index in range(520))
    await transport._write_fragmented_packet_locked(packet)
    assert client.writes == [packet[:500], packet[500:]]
    assert sleeps == [0.3]


async def test_ota_state_machine_follows_device_requested_chunk_size(
    transport: BleakTransport,
) -> None:
    await transport.connect()
    client = MockClient.instances[0]
    client.services = SimpleNamespace(
        get_characteristic=lambda _uuid: SimpleNamespace(
            max_write_without_response_size=500
        )
    )
    writes: list[bytes] = []

    async def ota_write(_uuid: str, packet: bytes, response: bool = False) -> None:
        writes.append(packet)
        if packet[2] == c.OTA_READY:
            client.notification_callback(None, reply(c.OTA_TRANSFER, b"\x00\x00\x10"))
        elif packet[2] == c.OTA_TRANSFER and packet[6] == 0:
            client.notification_callback(None, reply(c.OTA_TRANSFER, b"\x00\x00\x10"))

    client.write_gatt_char = ota_write
    image = bytes(range(40))
    result = await transport.ota_update(image, (1, 3, 0))
    assert result["bytes_sent"] == 40
    assert result["chunks_sent"] == 3
    assert writes[0][2] == c.OTA_READY
    assert writes[0][5:14] == len(image).to_bytes(4, "big") + b"\x01\x03\x00\x02\x00"
    payloads = [packet[5:-1] for packet in writes[1:]]
    assert [payload[:2] for payload in payloads] == [b"\x00\x00", b"\x00\x00", b"\x00\x01"]
    assert b"".join(payload[2:] for payload in payloads) == image


async def test_custom_chunk_timeout_keeps_session_for_same_chunk_retry(
    transport: BleakTransport,
) -> None:
    with pytest.raises(RequestTimeout, match="same chunk may retry"):
        await transport.request(c.PROCESS_CUSTOM_LIGHT_TRANSFER, b"chunk", timeout=0.01)
    assert transport.connected is True
    assert len(MockClient.instances) == 1


async def test_mutating_write_failure_is_unknown_and_invalidates_session(
    transport: BleakTransport,
) -> None:
    await transport.connect()
    MockClient.fail_write = True
    with pytest.raises(OutcomeUnknown, match="outcome is unknown"):
        await transport.request(c.SET_VOLUME, b"\x08")
    assert transport.connected is False


async def test_query_write_failure_is_transport_error_and_invalidates_session(
    transport: BleakTransport,
) -> None:
    await transport.connect()
    MockClient.fail_write = True
    with pytest.raises(TransportError, match="write failed"):
        await transport.request(c.QUERY_VOLUME)
    assert transport.connected is False


async def test_notification_queue_is_bounded(transport: BleakTransport) -> None:
    await transport.connect()
    callback = MockClient.instances[0].notification_callback
    for value in range(300):
        callback(None, reply(c.QUERY_VOLUME, bytes((16, value % 17))))
    assert transport._queue.qsize() == transport._NOTIFICATION_QUEUE_LIMIT
    events = await transport.events(limit=256)
    assert events["count"] == 256
    assert events["latest_sequence"] == 300
    assert events["dropped_before_retention"] == 44
    assert events["events"][-1]["opcode"] == c.QUERY_VOLUME


async def test_event_sessions_are_stable_and_obsolete_callbacks_are_ignored(
    transport: BleakTransport,
) -> None:
    await transport.connect()
    first_callback = MockClient.instances[0].notification_callback
    first_callback(None, reply(c.QUERY_VOLUME, b"\x10\x06"))
    await transport.disconnect()
    await transport.connect()
    second_callback = MockClient.instances[1].notification_callback
    second_callback(None, reply(c.QUERY_VOLUME, b"\x10\x07"))
    first_callback(None, reply(c.QUERY_VOLUME, b"\x10\x08"))
    events = await transport.events(limit=10)
    assert [event["session"] for event in events["events"]] == [1, 2]
    assert [event["payload_hex"] for event in events["events"]] == ["1006", "1007"]


async def test_scan_filters_in_process_without_bluez_uuid_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:01", name=None)
    advertisement = SimpleNamespace(
        manufacturer_data={c.EDIFIER_COMPANY_ID: bytes.fromhex("aabbccddeeff0200")},
        local_name=None,
        rssi=-55,
        service_uuids=[c.SEARCH_UUID],
    )

    class MockScanner:
        @staticmethod
        async def discover(**kwargs: object) -> dict[str, tuple[object, object]]:
            calls.append(kwargs)
            return {device.address: (device, advertisement)}

    monkeypatch.setattr(bleak, "BleakScanner", MockScanner)
    result = await BleakTransport(adapter="hci0").discover(0.01)

    assert len(result) == 1
    assert result[0].address == device.address
    assert calls == [{"timeout": 0.01, "return_adv": True, "adapter": "hci0"}]


async def test_scan_rejects_uuid_only_spoof(monkeypatch: pytest.MonkeyPatch) -> None:
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:02", name="not-edifier")
    advertisement = SimpleNamespace(
        manufacturer_data={},
        local_name="not-edifier",
        rssi=-30,
        service_uuids=[c.SEARCH_UUID],
    )

    class MockScanner:
        @staticmethod
        async def discover(**_kwargs: object) -> dict[str, tuple[object, object]]:
            return {device.address: (device, advertisement)}

    monkeypatch.setattr(bleak, "BleakScanner", MockScanner)
    assert await BleakTransport().discover(0.01) == []


async def test_connect_rejects_legacy_framing_version(
    transport: BleakTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:01", name=None)
    legacy = DeviceCandidate(
        address=device.address,
        name=None,
        rssi=-40,
        manufacturer_id=c.EDIFIER_COMPANY_ID,
        service_uuids=(c.SEARCH_UUID,),
        protocol_version=1,
        encryption_selector=0,
    )

    async def scan(_timeout: float) -> list[tuple[object, object, DeviceCandidate]]:
        return [(device, object(), legacy)]

    monkeypatch.setattr(transport, "_scan", scan)
    with pytest.raises(ProtocolError, match="framing version 1"):
        await transport.connect()
