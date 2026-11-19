from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class DeviceCandidate:
    address: str
    name: str | None
    rssi: int | None
    manufacturer_id: int | None
    service_uuids: tuple[str, ...]
    protocol_version: int | None = None
    encryption_selector: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeviceEvent:
    sequence: int
    session: int
    observed_at: str
    opcode: int | None
    payload: bytes
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "session": self.session,
            "observed_at": self.observed_at,
            "opcode": self.opcode,
            "opcode_hex": f"0x{self.opcode:02X}" if self.opcode is not None else None,
            "payload_hex": self.payload.hex(),
            "error": self.error,
        }


class Transport(ABC):
    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @property
    @abstractmethod
    def connection_identity(self) -> tuple[str, int] | None:
        """Return the current physical target and connection-session generation."""

    @abstractmethod
    async def discover(self, timeout: float) -> list[DeviceCandidate]: ...

    @abstractmethod
    async def connect(self, device: str | None = None) -> DeviceCandidate: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send(self, opcode: int, payload: bytes = b"") -> None:
        """Send a command without a response on the current connection."""

    @abstractmethod
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes:
        """Exchange a request on the current connection."""

    @abstractmethod
    async def events(self, since_sequence: int = 0, limit: int = 100) -> dict[str, object]:
        """Return retained notifications without initiating any Bluetooth operation."""

    @abstractmethod
    async def prepare_large_write(self, minimum_size: int) -> int:
        """Establish and return a validated one-write ATT payload capacity."""

    @abstractmethod
    async def ota_update(
        self,
        image: bytes,
        version: tuple[int, int, int],
    ) -> dict[str, object]:
        """Transfer one exact, prevalidated firmware image using Edifier OTA v2."""
