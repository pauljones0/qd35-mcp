from __future__ import annotations

from .. import constants as c
from ..errors import DeviceNotFound, OutcomeUnknown
from .base import DeviceCandidate, Transport


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
        self.name = "EDIFIER QD35 (simulated)"
        self.firmware = bytes((1, 3, 0))
        self.light_group = 1
        self.light_selected = 4
        self.light_records = self._default_light_records()
        self.bands = self._default_bands()
        self.device = DeviceCandidate(
            address="FA:CE:D3:50:00:01",
            name=self.name,
            rssi=-42,
            manufacturer_id=c.EDIFIER_COMPANY_ID,
            service_uuids=(c.SEARCH_UUID,),
            protocol_version=2,
            encryption_selector=0,
        )

    @property
    def connected(self) -> bool:
        return self._connected

    async def discover(self, timeout: float) -> list[DeviceCandidate]:
        return [self.device] if self.powered else []

    async def connect(self, device: str | None = None) -> DeviceCandidate:
        if not self.powered:
            raise DeviceNotFound("simulated speaker is powered off")
        if device and device not in {self.device.address, "fake"}:
            raise DeviceNotFound(f"simulated device {device!r} does not exist")
        self._connected = True
        return self.device

    async def disconnect(self) -> None:
        self._connected = False

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
        if opcode == c.QUERY_LIGHT:
            records = b"".join(bytes(record) for record in self.light_records.values())
            return bytes((self.light_group, self.light_selected)) + records
        if opcode == c.SET_LIGHT:
            self.light_group, self.light_selected = payload[:2]
            self.light_records[self.light_selected] = list(payload[1:])
            for record in self.light_records.values():
                record[4] = payload[5]
            return payload
        if opcode == c.QUERY_LDAC:
            return bytes((self.ldac,))
        if opcode == c.QUERY_PLAYBACK:
            return bytes((self.playback_state,))
        if opcode == c.SET_LDAC:
            self.ldac = payload[0]
            self._connected = False
            raise OutcomeUnknown("simulated Bluetooth restart after LDAC change")
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
