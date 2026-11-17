from __future__ import annotations

import asyncio
from typing import Any

from .. import constants as c
from ..errors import (
    DeviceNotFound,
    OutcomeUnknown,
    ProtocolError,
    RequestTimeout,
    TransportError,
)
from ..protocol import Frame, FrameStreamDecoder, encode_frame
from .base import DeviceCandidate, Transport


class BleakTransport(Transport):
    def __init__(self, adapter: str | None = None, scan_timeout: float = 8) -> None:
        self.adapter = adapter
        self.scan_timeout = scan_timeout
        self._client: Any = None
        self._device: Any = None
        self._candidate: DeviceCandidate | None = None
        self._queue: asyncio.Queue[Frame | ProtocolError] = asyncio.Queue()
        self._request_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._decoder = FrameStreamDecoder()
        self._xor_payload = False

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    @staticmethod
    def _candidate_from(device: Any, advertisement: Any) -> DeviceCandidate:
        manufacturer = advertisement.manufacturer_data.get(c.EDIFIER_COMPANY_ID, b"")
        return DeviceCandidate(
            address=device.address,
            name=device.name or advertisement.local_name,
            rssi=getattr(advertisement, "rssi", None),
            manufacturer_id=c.EDIFIER_COMPANY_ID if manufacturer else None,
            service_uuids=tuple(value.lower() for value in advertisement.service_uuids),
            protocol_version=manufacturer[-2] if len(manufacturer) >= 2 else None,
            encryption_selector=manufacturer[-1] if manufacturer else None,
        )

    async def _scan(self, timeout: float) -> list[tuple[Any, Any, DeviceCandidate]]:
        from bleak import BleakScanner

        # Do not pass BlueZ a UUID discovery filter here. BlueZ 5.87 can
        # segfault when a filtered LE discovery starts while this speaker has
        # an active Classic A2DP transport. Scan broadly and apply the same
        # a400 filter below in-process instead.
        kwargs: dict[str, object] = {}
        if self.adapter:
            kwargs["adapter"] = self.adapter
        try:
            found = await BleakScanner.discover(timeout=timeout, return_adv=True, **kwargs)
        except Exception as exc:
            raise TransportError(f"Bluetooth scan failed: {exc}") from exc
        matches = []
        for device, advertisement in found.values():
            candidate = self._candidate_from(device, advertisement)
            if c.SEARCH_UUID in candidate.service_uuids:
                matches.append((device, advertisement, candidate))
        return matches

    async def discover(self, timeout: float) -> list[DeviceCandidate]:
        return [candidate for _, _, candidate in await self._scan(timeout)]

    async def connect(self, device: str | None = None) -> DeviceCandidate:
        async with self._request_lock:
            return await self._connect_locked(device)

    async def _connect_locked(self, device: str | None = None) -> DeviceCandidate:
        async with self._lifecycle_lock:
            if (
                self.connected
                and self._candidate
                and (device is None or device.lower() == self._candidate.address.lower())
            ):
                return self._candidate
            await self._disconnect_locked()
            matches = await self._scan(self.scan_timeout)
            if device:
                matches = [item for item in matches if item[2].address.lower() == device.lower()]
            if not matches:
                raise DeviceNotFound("No QD35 advertising the a400 service was found")
            if len(matches) > 1 and not device:
                addresses = ", ".join(item[2].address for item in matches)
                raise DeviceNotFound(
                    f"Multiple QD35 candidates found; set QD35_DEVICE: {addresses}"
                )
            next_device, _, next_candidate = matches[0]
            selector = next_candidate.encryption_selector or 0
            if selector not in (0, 0x10):
                raise ProtocolError(f"unsupported QD35 encryption selector 0x{selector:02x}")
            xor_payload = selector == 0x10
            decoder = FrameStreamDecoder(xor_payload)
            queue: asyncio.Queue[Frame | ProtocolError] = asyncio.Queue()

            from bleak import BleakClient

            client: Any = None

            def on_disconnect(disconnected_client: Any) -> None:
                # An obsolete client's late callback must not clear a newer session.
                if self._client is disconnected_client:
                    self._client = None

            def on_notification(_characteristic: Any, data: bytearray) -> None:
                for item in decoder.feed(bytes(data)):
                    queue.put_nowait(item)

            kwargs: dict[str, object] = {
                "timeout": 15,
                "disconnected_callback": on_disconnect,
            }
            if self.adapter:
                kwargs["adapter"] = self.adapter
            try:
                client = BleakClient(next_device, **kwargs)
                await client.connect()
                await client.start_notify(c.READ_UUID, on_notification)
                if not client.is_connected:
                    raise TransportError("device disconnected during notification setup")
            except asyncio.CancelledError:
                if client is not None:
                    await self._cleanup_client(client)
                raise
            except Exception as exc:
                if client is not None:
                    await self._cleanup_client(client)
                raise TransportError(f"Bluetooth connection setup failed: {exc}") from exc

            # Publish only a fully connected, subscribed session.
            self._device = next_device
            self._candidate = next_candidate
            self._xor_payload = xor_payload
            self._decoder = decoder
            self._queue = queue
            self._client = client
            return next_candidate

    @staticmethod
    async def _cleanup_client(client: Any) -> None:
        if not getattr(client, "is_connected", False):
            return
        try:
            await client.stop_notify(c.READ_UUID)
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass

    async def disconnect(self) -> None:
        async with self._request_lock:
            async with self._lifecycle_lock:
                await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        client, self._client = self._client, None
        if client:
            await self._cleanup_client(client)
        while not self._queue.empty():
            self._queue.get_nowait()

    async def send(self, opcode: int, payload: bytes = b"") -> None:
        async with self._request_lock:
            if not self.connected:
                await self._connect_locked(self._candidate.address if self._candidate else None)
            packet = encode_frame(opcode, payload, self._xor_payload)
            try:
                await self._client.write_gatt_char(c.WRITE_UUID, packet, response=False)
            except asyncio.CancelledError:
                await self._invalidate_locked()
                raise
            except Exception as exc:
                if not self.connected:
                    await self._invalidate_locked()
                    raise OutcomeUnknown(
                        f"connection dropped while writing opcode 0x{opcode:02X}"
                    ) from exc
                raise TransportError(
                    f"Bluetooth write failed for opcode 0x{opcode:02X}: {exc}"
                ) from exc

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        async with self._request_lock:
            if not self.connected:
                await self._connect_locked(self._candidate.address if self._candidate else None)
            while not self._queue.empty():
                self._queue.get_nowait()
            packet = encode_frame(opcode, payload, self._xor_payload)
            try:
                await self._client.write_gatt_char(c.WRITE_UUID, packet, response=False)
            except asyncio.CancelledError:
                await self._invalidate_locked()
                raise
            except Exception as exc:
                if not self.connected:
                    await self._invalidate_locked()
                    raise OutcomeUnknown(
                        f"connection dropped while writing opcode 0x{opcode:02X}"
                    ) from exc
                raise TransportError(
                    f"Bluetooth write failed for opcode 0x{opcode:02X}: {exc}"
                ) from exc
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    disconnected = not self.connected
                    await self._invalidate_locked()
                    if disconnected:
                        raise OutcomeUnknown(
                            f"connection dropped after opcode 0x{opcode:02X}; "
                            "physical outcome unknown"
                        )
                    raise RequestTimeout(f"no response for opcode 0x{opcode:02X}")
                try:
                    item = await asyncio.wait_for(self._queue.get(), remaining)
                except asyncio.CancelledError:
                    await self._invalidate_locked()
                    raise
                except TimeoutError as exc:
                    disconnected = not self.connected
                    await self._invalidate_locked()
                    if disconnected:
                        raise OutcomeUnknown(
                            f"connection dropped after opcode 0x{opcode:02X}; "
                            "physical outcome unknown"
                        ) from exc
                    raise RequestTimeout(f"no response for opcode 0x{opcode:02X}") from exc
                if isinstance(item, ProtocolError):
                    await self._invalidate_locked()
                    raise item
                if item.opcode == opcode:
                    return item.payload

    async def _invalidate_locked(self) -> None:
        async with self._lifecycle_lock:
            await self._disconnect_locked()
