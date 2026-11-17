from __future__ import annotations

import asyncio
from typing import Any

from .. import constants as c
from ..errors import DeviceNotFound, OutcomeUnknown, ProtocolError, RequestTimeout
from ..protocol import Frame, FrameStreamDecoder, encode_frame
from .base import DeviceCandidate, Transport


class BleakTransport(Transport):
    def __init__(self, adapter: str | None = None, scan_timeout: float = 8) -> None:
        self.adapter = adapter
        self.scan_timeout = scan_timeout
        self._client: Any = None
        self._device: Any = None
        self._candidate: DeviceCandidate | None = None
        self._queue: asyncio.Queue[Frame] = asyncio.Queue()
        self._lock = asyncio.Lock()
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

        kwargs: dict[str, object] = {"service_uuids": [c.SEARCH_UUID]}
        if self.adapter:
            kwargs["adapter"] = self.adapter
        found = await BleakScanner.discover(timeout=timeout, return_adv=True, **kwargs)
        matches = []
        for device, advertisement in found.values():
            candidate = self._candidate_from(device, advertisement)
            if c.SEARCH_UUID in candidate.service_uuids:
                matches.append((device, advertisement, candidate))
        return matches

    async def discover(self, timeout: float) -> list[DeviceCandidate]:
        return [candidate for _, _, candidate in await self._scan(timeout)]

    async def connect(self, device: str | None = None) -> DeviceCandidate:
        if (
            self.connected
            and self._candidate
            and (device is None or device == self._candidate.address)
        ):
            return self._candidate
        await self.disconnect()
        matches = await self._scan(self.scan_timeout)
        if device:
            matches = [item for item in matches if item[2].address.lower() == device.lower()]
        if not matches:
            raise DeviceNotFound("No QD35 advertising the a400 service was found")
        if len(matches) > 1 and not device:
            addresses = ", ".join(item[2].address for item in matches)
            raise DeviceNotFound(f"Multiple QD35 candidates found; set QD35_DEVICE: {addresses}")
        self._device, _, self._candidate = matches[0]
        selector = self._candidate.encryption_selector or 0
        if selector not in (0, 0x10):
            raise ProtocolError(f"unsupported QD35 encryption selector 0x{selector:02x}")
        self._xor_payload = selector == 0x10
        self._decoder = FrameStreamDecoder(self._xor_payload)

        from bleak import BleakClient

        kwargs: dict[str, object] = {"timeout": 15, "disconnected_callback": self._on_disconnect}
        if self.adapter:
            kwargs["adapter"] = self.adapter
        self._client = BleakClient(self._device, **kwargs)
        await self._client.connect()
        await self._client.start_notify(c.READ_UUID, self._on_notification)
        return self._candidate

    def _on_disconnect(self, _client: Any) -> None:
        self._client = None

    def _on_notification(self, _characteristic: Any, data: bytearray) -> None:
        try:
            for frame in self._decoder.feed(bytes(data)):
                self._queue.put_nowait(frame)
        except ProtocolError:
            # A malformed notification cannot safely satisfy a pending request.
            return

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client and client.is_connected:
            try:
                await client.stop_notify(c.READ_UUID)
            except Exception:
                pass
            await client.disconnect()
        while not self._queue.empty():
            self._queue.get_nowait()

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        async with self._lock:
            if not self.connected:
                await self.connect(self._candidate.address if self._candidate else None)
            while not self._queue.empty():
                self._queue.get_nowait()
            packet = encode_frame(opcode, payload, self._xor_payload)
            try:
                await self._client.write_gatt_char(c.WRITE_UUID, packet, response=False)
            except Exception as exc:
                if not self.connected:
                    raise OutcomeUnknown(
                        f"connection dropped while writing opcode 0x{opcode:02X}"
                    ) from exc
                raise
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    if not self.connected:
                        raise OutcomeUnknown(
                            f"connection dropped after opcode 0x{opcode:02X}; "
                            "physical outcome unknown"
                        )
                    raise RequestTimeout(f"no response for opcode 0x{opcode:02X}")
                try:
                    frame = await asyncio.wait_for(self._queue.get(), remaining)
                except TimeoutError as exc:
                    if not self.connected:
                        raise OutcomeUnknown(
                            f"connection dropped after opcode 0x{opcode:02X}; "
                            "physical outcome unknown"
                        ) from exc
                    raise RequestTimeout(f"no response for opcode 0x{opcode:02X}") from exc
                if frame.opcode == opcode:
                    return frame.payload
