import math

import pytest

from qd35_mcp.config import Config


def make_config(**changes: object) -> Config:
    values = {
        "device": None,
        "adapter": None,
        "transport": "fake",
        "writes_enabled": True,
        "dangerous_writes_enabled": False,
        "request_timeout": 4.0,
        "scan_timeout": 8.0,
    }
    values.update(changes)
    return Config(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, 61, math.inf, math.nan])
def test_request_timeout_is_bounded(value: float) -> None:
    with pytest.raises(ValueError, match="request_timeout"):
        make_config(request_timeout=value)


@pytest.mark.parametrize("value", [0, -1, 301, math.inf, math.nan])
def test_scan_timeout_is_bounded(value: float) -> None:
    with pytest.raises(ValueError, match="scan_timeout"):
        make_config(scan_timeout=value)


def test_environment_identifiers_are_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QD35_TRANSPORT", "fake")
    monkeypatch.setenv("QD35_DEVICE", " AA:BB:CC:DD:EE:FF ")
    monkeypatch.setenv("QD35_ADAPTER", " hci1 ")
    config = Config.from_env()
    assert config.device == "AA:BB:CC:DD:EE:FF"
    assert config.adapter == "hci1"


def test_invalid_boolean_environment_value_is_not_silently_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QD35_TRANSPORT", "fake")
    monkeypatch.setenv("QD35_ENABLE_WRITES", "treu")
    with pytest.raises(ValueError, match="QD35_ENABLE_WRITES"):
        Config.from_env()
