from dataclasses import replace

import pytest

from qd35_mcp.config import Config
from qd35_mcp.errors import DangerousWritesDisabled
from qd35_mcp.service import QD35Service
from qd35_mcp.transport.fake import FakeTransport


@pytest.fixture
def config() -> Config:
    return Config(
        device=None,
        adapter=None,
        transport="fake",
        writes_enabled=True,
        dangerous_writes_enabled=False,
        request_timeout=1,
        scan_timeout=1,
    )


@pytest.fixture
def service(config: Config) -> QD35Service:
    return QD35Service(FakeTransport(), config)


async def test_status_reports_complete_simulated_state(service: QD35Service) -> None:
    status = await service.status()
    assert status["volume"] == {"maximum": 16, "current": 6}
    assert status["input"]["name"] == "bluetooth"
    assert status["lighting"]["selected_effect"] == "breathing"


async def test_semantic_changes_verify_readback(service: QD35Service) -> None:
    assert (await service.set_volume(9))["applied"] is True
    assert (await service.set_input("usb"))["applied"] is True
    assert (await service.set_eq_preset("movie"))["applied"] is True
    eq = await service.set_eq_band(2, 1.5)
    assert eq["applied"] is True
    assert eq["active_preset"]["name"] == "diy"
    light = await service.set_light("static", "#FF8800", 40)
    assert light["applied"] is True


async def test_dangerous_operations_are_separately_gated(config: Config) -> None:
    service = QD35Service(FakeTransport(), config)
    with pytest.raises(DangerousWritesDisabled):
        await service.factory_reset()
    enabled = QD35Service(FakeTransport(), replace(config, dangerous_writes_enabled=True))
    assert (await enabled.factory_reset())["sent"] is True
