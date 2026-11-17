import asyncio
from types import SimpleNamespace

import bleak
import pytest

from qd35_mcp import constants as c
from qd35_mcp.errors import RequestTimeout, TransportError
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
        self.writes.append(packet)
        if self.auto_reply and self.notification_callback:
            self.notification_callback(None, reply(packet[2], b"\x10\x06"))


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> BleakTransport:
    MockClient.instances = []
    MockClient.fail_notify = False
    MockClient.auto_reply = False
    MockClient.ready_event = asyncio.Event()
    monkeypatch.setattr(bleak, "BleakClient", MockClient)
    result = BleakTransport(scan_timeout=0.01)
    device = SimpleNamespace(address="64:68:70:64:37:0D", name=None)
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


async def test_scan_filters_in_process_without_bluez_uuid_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    device = SimpleNamespace(address="64:68:70:64:37:0D", name=None)
    advertisement = SimpleNamespace(
        manufacturer_data={c.EDIFIER_COMPANY_ID: b"dhvd7\r\x02\x00"},
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
