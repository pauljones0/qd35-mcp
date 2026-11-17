from __future__ import annotations

import os
from dataclasses import dataclass


def _enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Config:
    device: str | None
    adapter: str | None
    transport: str
    writes_enabled: bool
    dangerous_writes_enabled: bool
    request_timeout: float
    scan_timeout: float

    @classmethod
    def from_env(cls) -> Config:
        transport = os.getenv("QD35_TRANSPORT", "bleak").strip().lower()
        if transport not in {"bleak", "fake"}:
            raise ValueError("QD35_TRANSPORT must be 'bleak' or 'fake'")
        return cls(
            device=os.getenv("QD35_DEVICE") or None,
            adapter=os.getenv("QD35_ADAPTER") or None,
            transport=transport,
            writes_enabled=_enabled("QD35_ENABLE_WRITES", transport == "fake"),
            dangerous_writes_enabled=_enabled("QD35_ENABLE_DANGEROUS"),
            request_timeout=float(os.getenv("QD35_REQUEST_TIMEOUT", "4")),
            scan_timeout=float(os.getenv("QD35_SCAN_TIMEOUT", "8")),
        )
