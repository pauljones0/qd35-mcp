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


class Transport(ABC):
    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    async def discover(self, timeout: float) -> list[DeviceCandidate]: ...

    @abstractmethod
    async def connect(self, device: str | None = None) -> DeviceCandidate: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def request(self, opcode: int, payload: bytes = b"", timeout: float = 4) -> bytes: ...
