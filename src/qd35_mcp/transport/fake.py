from __future__ import annotations

from datetime import datetime

from .. import constants as c
from ..display import (
    DISPLAY_DEFAULT_FLAGS,
    DISPLAY_MAGIC,
    DISPLAY_RESET,
    DISPLAY_SET_STATE,
    DISPLAY_WRITE_GRID,
)
from ..errors import DeviceNotFound, OutcomeUnknown
from .base import DeviceCandidate, DeviceEvent, Transport


class FakeTransport(Transport):
    """Deterministic QD35 simulator for setup, agent evaluation, and tests."""

    @staticmethod
    def _default_light_records() -> dict[int, list[int]]:
        return {
            1: [1, 1, 0, 0, 100, 5],
            2: [2, 1, 0, 0, 100, 3],
            3: [3, 230, 230, 255, 100, 255],
            4: [4, 230, 230, 255, 100, 3],
            5: [5, 180, 118, 255, 100, 255],
            6: [6, 230, 230, 255, 100, 255],
            7: [7, 0, 0, 0, 100, 255],
        }

    @staticmethod
    def _default_bands() -> list[list[int]]:
        return [
            [0, 0, 0, 62, 6, 7],
            [1, 0, 0, 250, 6, 7],
            [2, 0, 3, 232, 6, 7],
            [3, 0, 15, 160, 6, 7],
            [4, 0, 31, 64, 6, 7],
            [5, 0, 62, 128, 6, 7],
        ]

    def __init__(self) -> None:
        self._connected = False
        self._session = 0
        self.powered = True
        self.audio_connected = True
        self.volume = 6
        self.maximum = 16
        self.input_group = 2
        self.input_value = 1
        self.eq_preset = 0
        self.ldac = 0
        self.last_media_action: int | None = None
        self.playback_state = 3
        self.last_clock_payload: bytes | None = None
        self.audio_status = 1
        self.custom_light_status = (0, 0)
        self.custom_light_transfer: bytearray | None = None
        self.custom_light_id = 0
        self.custom_light_palette = 0
        self.custom_light_sequences: set[int] = set()
        self.custom_light_bytes_written = 0
        self.custom_light_type: int | None = None
        self.light_timer = (0, 0)
        self.name = "EDIFIER QD35 (simulated)"
        self.firmware = bytes((1, 3, 0))
        self.light_group = 1
        self.light_selected = 4
        self.light_records = self._default_light_records()
        self.display_flags = DISPLAY_DEFAULT_FLAGS
        self.display_brightness = 7
        self.display_grid = [0xFF] * 8
        self.bands = self._default_bands()
        self._events: list[DeviceEvent] = []
        self._event_sequence = 0
        self.device = DeviceCandidate(
            address="FA:CE:D3:50:00:01",
            name=self.name,
            rssi=-42,
            manufacturer_id=c.EDIFIER_COMPANY_ID,
            service_uuids=(c.SEARCH_UUID,),
            protocol_version=2,
            encryption_selector=0,
        )
        self.last_ota_image: bytes | None = None
        self.last_ota_version: tuple[int, int, int] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def connection_identity(self) -> tuple[str, int] | None:
        return (self.device.address, self._session) if self._connected else None

    async def discover(self, timeout: float) -> list[DeviceCandidate]:
        return [self.device] if self.powered else []

    async def connect(self, device: str | None = None) -> DeviceCandidate:
        if not self.powered:
            raise DeviceNotFound("simulated speaker is powered off")
        if device and device not in {self.device.address, "fake"}:
            raise DeviceNotFound(f"simulated device {device!r} does not exist")
        if not self._connected:
            self._session += 1
            self._connected = True
        return self.device

    async def disconnect(self) -> None:
        self._connected = False

    async def prepare_large_write(self, minimum_size: int) -> int:
        if not self._connected:
            await self.connect()
        return 509

    async def ota_update(
        self,
        image: bytes,
        version: tuple[int, int, int],
    ) -> dict[str, object]:
        if not self._connected:
            await self.connect()
        self.last_ota_image = image
        self.last_ota_version = version
        return {
            "bytes_sent": len(image),
            "chunks_sent": (len(image) + 511) // 512,
            "target_version": ".".join(map(str, version)),
            "final_chunk_sent": True,
            "completion_readback": False,
        }

    def _emit(self, opcode: int, payload: bytes) -> None:
        self._event_sequence += 1
        self._events.append(
            DeviceEvent(
                sequence=self._event_sequence,
                session=1,
                observed_at=datetime.now().astimezone().isoformat(),
                opcode=opcode,
                payload=payload,
            )
        )

    async def send(self, opcode: int, payload: bytes = b"") -> None:
        if not self.powered:
            raise DeviceNotFound("simulated speaker is powered off")
        if not self._connected:
            await self.connect()
        if opcode == c.MEDIA_CONTROL:
            self.last_media_action = payload[0]
            if payload[0] == c.MEDIA_ACTIONS["play"]:
                self.playback_state = 0x0D
            elif payload[0] == c.MEDIA_ACTIONS["pause"]:
                self.playback_state = 3
            self._emit(c.QUERY_PLAYBACK, bytes((self.playback_state,)))

    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        if not self.powered:
            raise DeviceNotFound("simulated speaker is powered off")
        if not self._connected:
            await self.connect()
        if opcode == c.QUERY_VOLUME:
            return bytes((self.maximum, self.volume))
        if opcode == c.SET_VOLUME:
            self.volume = payload[0]
            return bytes((self.maximum, self.volume))
        if opcode == c.QUERY_INPUT:
            return bytes((self.input_group, self.input_value))
        if opcode == c.SET_INPUT:
            self.input_group, self.input_value = payload[:2]
            return bytes((1,))
        if opcode == c.QUERY_EQ_PRESET:
            return bytes((self.eq_preset,))
        if opcode == c.SET_EQ_PRESET:
            self.eq_preset = payload[0]
            return bytes((self.eq_preset,))
        if opcode == c.QUERY_EQ:
            return bytes((3, len(self.bands))) + b"".join(bytes(band) for band in self.bands)
        if opcode == c.SET_EQ:
            index = payload[0]
            self.bands[index] = list(payload[:6])
            return bytes((1,))
        if opcode == c.RESET_EQ:
            self.bands = self._default_bands()
            return b"\x01"
        if opcode == c.QUERY_LIGHT:
            records = b"".join(bytes(record) for record in self.light_records.values())
            return bytes((self.light_group, self.light_selected)) + records
        if opcode == c.QUERY_AUDIO_STATUS:
            return bytes((self.audio_status,))
        if opcode == c.QUERY_CUSTOM_LIGHT:
            return bytes(self.custom_light_status)
        if opcode == c.START_CUSTOM_LIGHT_TRANSFER:
            self.custom_light_id = payload[0]
            self.custom_light_palette = payload[1] if len(payload) > 1 else 0
            self.custom_light_transfer = bytearray()
            self.custom_light_sequences = set()
            self.custom_light_bytes_written = 0
            return bytes.fromhex("01030000009b00010000")
        if opcode == c.PROCESS_CUSTOM_LIGHT_TRANSFER:
            if self.custom_light_transfer is None:
                return b"\x01\x00"
            sequence = payload[2]
            self.custom_light_type = payload[0]
            if sequence not in self.custom_light_sequences:
                self.custom_light_transfer.extend(payload[5:-2])
                self.custom_light_sequences.add(sequence)
                self.custom_light_bytes_written += len(payload[5:-2])
            return bytes((0, sequence - 1))
        if opcode == c.FINISH_CUSTOM_LIGHT_TRANSFER:
            if payload == b"\x01" and self.custom_light_transfer is not None:
                self.custom_light_status = (
                    self.custom_light_id,
                    self.custom_light_palette,
                )
                self.custom_light_transfer = None
                return b"\x01"
            self.custom_light_transfer = None
            return b"\x01"
        if opcode == c.QUERY_LIGHT_TIMER:
            return bytes(self.light_timer)
        if opcode == c.SET_LIGHT_TIMER:
            self.light_timer = (payload[0], payload[1])
            return bytes(self.light_timer)
        if opcode == c.SET_LIGHT:
            if len(payload) == 7 and payload[1] == 0 and payload[6] == DISPLAY_MAGIC:
                if payload[2] == DISPLAY_SET_STATE:
                    self.display_flags = payload[3] & 0x3F
                    self.display_brightness = payload[4]
                elif payload[2] == DISPLAY_WRITE_GRID:
                    self.display_grid[payload[3]] = payload[4]
                elif payload[2] == DISPLAY_RESET:
                    self.display_flags = DISPLAY_DEFAULT_FLAGS
                    self.display_brightness = 7
                    self.display_grid = [0xFF] * 8
                return payload
            self.light_group, self.light_selected = payload[:2]
            self.light_records[self.light_selected] = list(payload[1:])
            for record in self.light_records.values():
                record[4] = payload[5]
            return payload
        if opcode == c.RESET_LIGHT:
            self.light_group, self.light_selected = payload[:2]
            self.light_records = self._default_light_records()
            return bytes((self.light_group, self.light_selected)) + b"".join(
                bytes(record) for record in self.light_records.values()
            )
        if opcode == c.QUERY_LDAC:
            return bytes((self.ldac,))
        if opcode == c.QUERY_PLAYBACK:
            return bytes((self.playback_state,))
        if opcode == c.SET_LDAC:
            self.ldac = payload[0]
            self._connected = False
            raise OutcomeUnknown(
                "simulated Bluetooth restart after LDAC change", connection_dropped=True
            )
        if opcode == c.QUERY_FIRMWARE:
            return self.firmware
        if opcode == c.QUERY_NAME:
            return self.name.encode()
        if opcode == c.SET_NAME:
            self.name = payload.decode()
            return b"\x01"
        if opcode == c.QUERY_ADDRESS:
            return bytes.fromhex("aabbccddeeff")
        if opcode == c.QUERY_CAPABILITIES:
            return bytes.fromhex("0000010401010100000001010a0c0a00002004830401")
        if opcode == c.POWER_OFF:
            self.powered = False
            self.audio_connected = False
            self._connected = False
            return b"\x01"
        if opcode == c.FACTORY_RESET:
            self.audio_connected = False
            self._connected = False
            self.volume = 6
            self.input_group = 2
            self.input_value = 1
            self.eq_preset = 0
            self.ldac = 0
            self.name = "EDIFIER QD35 (simulated)"
            self.light_group = 1
            self.light_selected = 4
            self.light_records = self._default_light_records()
            self.bands = self._default_bands()
            self.playback_state = 3
            return b"\x01"
        if opcode == c.SYNC_CLOCK:
            self.last_clock_payload = payload
            return b"\x01"
        if opcode == c.DISCONNECT_AUDIO:
            self.audio_connected = False
            self._connected = False
            return b"\x01"
        return b""

    async def events(self, since_sequence: int = 0, limit: int = 100) -> dict[str, object]:
        selected = [event for event in self._events if event.sequence > since_sequence][:limit]
        return {
            "events": [event.to_dict() for event in selected],
            "count": len(selected),
            "latest_sequence": self._event_sequence,
            "more": False,
            "dropped_before_retention": 0,
            "note": "simulated event stream",
        }
