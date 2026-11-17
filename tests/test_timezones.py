from __future__ import annotations

import pytest

from qd35_mcp.timezones import persist_timezone, resolve_timezone, validate_timezone


def test_timezone_persistence_overrides_system_detection(tmp_path) -> None:
    path = tmp_path / "timezone"
    assert persist_timezone("America/Regina", path) == "America/Regina"
    assert path.read_text(encoding="utf-8") == "America/Regina\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert resolve_timezone(None, path) == "America/Regina"
    assert resolve_timezone("America/Toronto", path) == "America/Toronto"


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        validate_timezone("Mars/Olympus_Mons")
