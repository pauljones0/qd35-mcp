from __future__ import annotations

import pytest

from qd35_mcp import audio_keepalive
from qd35_mcp.ownership import SpeakerOwnership


async def test_keepalive_connects_even_when_qd35_pairing_is_ephemeral(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_bluetoothctl(*arguments: str) -> tuple[int, str]:
        calls.append(arguments)
        if arguments[0] == "info":
            return 0, "\tPaired: no\n\tTrusted: yes\n\tConnected: no\n"
        raise RuntimeError("stop after reconnect attempt")

    monkeypatch.setattr(audio_keepalive, "_bluetoothctl", fake_bluetoothctl)
    ownership = SpeakerOwnership(tmp_path / "owner")
    await ownership.set_manual("AA:BB:CC:DD:EE:FF", True)
    with pytest.raises(RuntimeError, match="stop after reconnect"):
        await audio_keepalive.maintain_device(ownership, "AA:BB:CC:DD:EE:FF")
    assert calls == [
        ("info", "AA:BB:CC:DD:EE:FF"),
        ("connect", "AA:BB:CC:DD:EE:FF"),
    ]
