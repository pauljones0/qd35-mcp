"""Construct the same speaker service for the MCP, alarm runner, and local tools."""

from .config import Config
from .ownership import SpeakerOwnership
from .service import QD35Service
from .transport.bleak import BleakTransport
from .transport.fake import FakeTransport


def create_service(config: Config | None = None) -> QD35Service:
    config = config or Config.from_env()
    transport = (
        FakeTransport()
        if config.transport == "fake"
        else BleakTransport(adapter=config.adapter, scan_timeout=config.scan_timeout)
    )
    ownership = SpeakerOwnership() if config.transport == "bleak" else None
    return QD35Service(transport, config, ownership)
