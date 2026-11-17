from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
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
from .base import DeviceCandidate, DeviceEvent, Transport


class BleakTransport(Transport):
    _NOTIFICATION_QUEUE_LIMIT = 256

    def __init__(self, adapter: str | None = None, scan_timeout: float = 8) -> None:
        self.adapter = adapter
        self.scan_timeout = scan_timeout
        self._client: Any = None
        self._device: Any = None
        self._candidate: DeviceCandidate | None = None
        self._queue: asyncio.Queue[Frame | ProtocolError] = asyncio.Queue(
            maxsize=self._NOTIFICATION_QUEUE_LIMIT
        )
        self._request_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._decoder = FrameStreamDecoder()
        self._xor_payload = False
        self._events: deque[DeviceEvent] = deque(maxlen=self._NOTIFICATION_QUEUE_LIMIT)
        self._event_sequence = 0
        self._event_overflow = 0
        self._session = 0
        self._mtu_ready = False

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    @property
    def connection_identity(self) -> tuple[str, int] | None:
        if not self.connected or self._candidate is None:
            return None
        return (self._candidate.address, self._session)

    @staticmethod
    def _candidate_from(device: Any, advertisement: Any) -> DeviceCandidate:
        manufacturer = advertisement.manufacturer_data.get(c.EDIFIER_COMPANY_ID, b"")
        has_identity = len(manufacturer) >= 8
        return DeviceCandidate(
            address=device.address,
            name=device.name or advertisement.local_name,
            rssi=getattr(advertisement, "rssi", None),
            manufacturer_id=c.EDIFIER_COMPANY_ID if has_identity else None,
            service_uuids=tuple(value.lower() for value in advertisement.service_uuids),
            protocol_version=manufacturer[-2] if has_identity else None,
            encryption_selector=manufacturer[-1] if has_identity else None,
        )

    @staticmethod
    def _error_detail(exc: Exception) -> str:
        return str(exc).strip() or type(exc).__name__

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
            raise TransportError(f"Bluetooth scan failed: {self._error_detail(exc)}") from exc
        matches = []
        for device, advertisement in found.values():
            candidate = self._candidate_from(device, advertisement)
            if (
                c.SEARCH_UUID in candidate.service_uuids
                and candidate.manufacturer_id == c.EDIFIER_COMPANY_ID
            ):
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
            if next_candidate.protocol_version != 2:
                raise ProtocolError(
                    f"unsupported QD35 framing version {next_candidate.protocol_version!r}"
                )
            selector = next_candidate.encryption_selector or 0
            if selector not in (0, 0x10):
                raise ProtocolError(f"unsupported QD35 encryption selector 0x{selector:02x}")
            xor_payload = selector == 0x10
            decoder = FrameStreamDecoder(xor_payload)
            next_session = self._session + 1
            queue: asyncio.Queue[Frame | ProtocolError] = asyncio.Queue(
                maxsize=self._NOTIFICATION_QUEUE_LIMIT
            )

            from bleak import BleakClient

            client: Any = None

            def on_disconnect(disconnected_client: Any) -> None:
                # An obsolete client's late callback must not clear a newer session.
                if self._client is disconnected_client:
                    self._client = None

            def on_notification(_characteristic: Any, data: bytearray) -> None:
                initializing = self._client is None and self._session == next_session - 1
                current = self._client is client and self._session == next_session
                if not (initializing or current):
                    return
                for item in decoder.feed(bytes(data)):
                    self._event_sequence += 1
                    if len(self._events) == self._events.maxlen:
                        self._event_overflow += 1
                    self._events.append(
                        DeviceEvent(
                            sequence=self._event_sequence,
                            session=next_session,
                            observed_at=datetime.now().astimezone().isoformat(),
                            opcode=item.opcode if isinstance(item, Frame) else None,
                            payload=item.payload if isinstance(item, Frame) else b"",
                            error=str(item) if isinstance(item, ProtocolError) else None,
                        )
                    )
                    if queue.full():
                        queue.get_nowait()
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
                raise TransportError(
                    f"Bluetooth connection setup failed: {self._error_detail(exc)}"
                ) from exc

            # Publish only a fully connected, subscribed session.
            self._device = next_device
            self._candidate = next_candidate
            self._xor_payload = xor_payload
            self._decoder = decoder
            self._queue = queue
            self._session = next_session
            self._mtu_ready = False
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
                await self._write_packet_locked(packet)
            except asyncio.CancelledError:
                await self._invalidate_locked()
                raise
            except Exception as exc:
                disconnected = not self.connected
                await self._invalidate_locked()
                raise OutcomeUnknown(
                    f"Bluetooth write outcome is unknown for opcode 0x{opcode:02X}: "
                    f"{self._error_detail(exc)}",
                    connection_dropped=disconnected,
                ) from exc

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        async with self._request_lock:
            if not self.connected:
                await self._connect_locked(self._candidate.address if self._candidate else None)
            while not self._queue.empty():
                self._queue.get_nowait()
            packet = encode_frame(opcode, payload, self._xor_payload)
            try:
                await self._write_packet_locked(packet)
            except asyncio.CancelledError:
                await self._invalidate_locked()
                raise
            except Exception as exc:
                disconnected = not self.connected
                await self._invalidate_locked()
                if opcode in c.MUTATING_OPCODES:
                    raise OutcomeUnknown(
                        f"Bluetooth write outcome is unknown for opcode 0x{opcode:02X}: "
                        f"{self._error_detail(exc)}",
                        connection_dropped=disconnected,
                    ) from exc
                raise TransportError(
                    f"Bluetooth write failed for opcode 0x{opcode:02X}: {self._error_detail(exc)}",
                    connection_dropped=disconnected,
                ) from exc
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    disconnected = not self.connected
                    if opcode == c.PROCESS_CUSTOM_LIGHT_TRANSFER and not disconnected:
                        raise RequestTimeout(
                            "no custom-light chunk acknowledgement; the same chunk may retry"
                        )
                    await self._invalidate_locked()
                    if disconnected or opcode in c.MUTATING_OPCODES:
                        raise OutcomeUnknown(
                            f"no valid confirmation for opcode 0x{opcode:02X}; "
                            "physical write outcome unknown",
                            connection_dropped=disconnected,
                        )
                    raise RequestTimeout(
                        f"no response for opcode 0x{opcode:02X}",
                        connection_dropped=disconnected,
                    )
                try:
                    item = await asyncio.wait_for(self._queue.get(), remaining)
                except asyncio.CancelledError:
                    await self._invalidate_locked()
                    raise
                except TimeoutError as exc:
                    disconnected = not self.connected
                    if opcode == c.PROCESS_CUSTOM_LIGHT_TRANSFER and not disconnected:
                        raise RequestTimeout(
                            "no custom-light chunk acknowledgement; the same chunk may retry"
                        ) from exc
                    await self._invalidate_locked()
                    if disconnected or opcode in c.MUTATING_OPCODES:
                        raise OutcomeUnknown(
                            f"no valid confirmation for opcode 0x{opcode:02X}; "
                            "physical write outcome unknown",
                            connection_dropped=disconnected,
                        ) from exc
                    raise RequestTimeout(
                        f"no response for opcode 0x{opcode:02X}",
                        connection_dropped=disconnected,
                    ) from exc
                if isinstance(item, ProtocolError):
                    disconnected = not self.connected
                    await self._invalidate_locked()
                    if opcode in c.MUTATING_OPCODES:
                        raise OutcomeUnknown(
                            f"invalid confirmation for opcode 0x{opcode:02X}; "
                            "physical write outcome unknown",
                            connection_dropped=disconnected,
                        ) from item
                    raise item
                if item.opcode == opcode:
                    return item.payload

    async def _write_packet_locked(self, packet: bytes) -> None:
        """Write one complete protocol frame after validating ATT capacity."""
        services = getattr(self._client, "services", None)
        characteristic = services.get_characteristic(c.WRITE_UUID) if services else None
        maximum = int(getattr(characteristic, "max_write_without_response_size", 20) or 20)
        if len(packet) > maximum:
            maximum = await self._prepare_large_write_locked(len(packet))
        if len(packet) > maximum:
            raise TransportError(
                f"negotiated ATT write capacity {maximum} is below frame size {len(packet)}"
            )
        await self._client.write_gatt_char(c.WRITE_UUID, packet, response=False)

    async def prepare_large_write(self, minimum_size: int) -> int:
        if minimum_size < 1:
            raise ValueError("minimum_size must be positive")
        async with self._request_lock:
            if not self.connected:
                await self._connect_locked(self._candidate.address if self._candidate else None)
            return await self._prepare_large_write_locked(minimum_size)

    async def _prepare_large_write_locked(self, minimum_size: int) -> int:
        services = getattr(self._client, "services", None)
        characteristic = services.get_characteristic(c.WRITE_UUID) if services else None
        maximum = int(getattr(characteristic, "max_write_without_response_size", 20) or 20)
        if maximum >= minimum_size:
            return maximum
        acquire_mtu = getattr(getattr(self._client, "_backend", None), "_acquire_mtu", None)
        if acquire_mtu is None:
            raise TransportError(
                f"Bluetooth backend cannot negotiate the {minimum_size}-byte write required"
            )
        if not self._mtu_ready:
            try:
                await acquire_mtu()
            except Exception as exc:
                raise TransportError(
                    f"Bluetooth MTU negotiation failed: {self._error_detail(exc)}"
                ) from exc
            self._mtu_ready = True
            # Edifier Connect waits two seconds after requesting MTU 512.
            await asyncio.sleep(2)
        maximum = int(
            getattr(characteristic, "max_write_without_response_size", maximum) or maximum
        )
        negotiated_mtu = int(getattr(self._client, "mtu_size", 23) or 23)
        maximum = max(maximum, negotiated_mtu - 3)
        if maximum < minimum_size:
            raise TransportError(
                f"negotiated ATT write capacity {maximum} is below required {minimum_size}"
            )
        return maximum

    async def _invalidate_locked(self) -> None:
        async with self._lifecycle_lock:
            await self._disconnect_locked()

    async def events(self, since_sequence: int = 0, limit: int = 100) -> dict[str, object]:
        if since_sequence < 0:
            raise ValueError("since_sequence must be nonnegative")
        if not 1 <= limit <= self._NOTIFICATION_QUEUE_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {self._NOTIFICATION_QUEUE_LIMIT}"
            )
        retained = [event for event in self._events if event.sequence > since_sequence]
        selected = retained[:limit]
        return {
            "events": [event.to_dict() for event in selected],
            "count": len(selected),
            "latest_sequence": self._event_sequence,
            "more": len(retained) > len(selected),
            "dropped_before_retention": self._event_overflow,
            "note": (
                "The protocol has no transaction ID; a same-opcode notification can be "
                "ambiguous between a command reply and an unsolicited state event."
            ),
        }
