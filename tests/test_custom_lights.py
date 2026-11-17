import hashlib

import pytest

from qd35_mcp import custom_lights
from qd35_mcp.custom_lights import CustomLightAsset, apply_palette
from qd35_mcp.errors import ProtocolError


class Response:
    def __init__(
        self,
        data: bytes,
        url: str = "https://edifier-us-app.s3.us-east-2.amazonaws.com/test.bin",
    ) -> None:
        self.data = data
        self.url = url

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.data[:limit]

    def geturl(self) -> str:
        return self.url


def test_palette_transformation_matches_app_integer_scaling() -> None:
    record = bytearray((0, 20))
    record.extend((255, 128, 64, 0) * 37)
    transformed = apply_palette(bytes(record), (125, 130, 255, 1))
    assert transformed[:2] == b"\x00\x14"
    assert transformed[2:6] == bytes((125, 65, 64, 0))


def test_asset_download_is_hash_and_structure_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    data = bytes((0, 20)) + bytes((255, 0, 0, 0)) * 37
    asset = CustomLightAsset(
        "test",
        99,
        1,
        "https://edifier-us-app.s3.us-east-2.amazonaws.com/test.bin",
        hashlib.sha256(data).hexdigest(),
    )
    monkeypatch.setitem(custom_lights.CUSTOM_LIGHTS, "test", asset)
    monkeypatch.setattr(custom_lights, "_open_asset", lambda *_args, **_kwargs: Response(data))
    fetched_asset, fetched_data = custom_lights.fetch_custom_light_asset("test")
    assert fetched_asset == asset
    assert fetched_data == data

    monkeypatch.setattr(
        custom_lights,
        "_open_asset",
        lambda *_args, **_kwargs: Response(data[:-1] + b"\x01"),
    )
    with pytest.raises(ProtocolError, match="hash mismatch"):
        custom_lights.fetch_custom_light_asset("test")


def test_asset_download_rejects_non_allowlisted_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = custom_lights.CUSTOM_LIGHTS["spiral"]
    data = bytes((0, 100)) + bytes((255, 255, 255, 0)) * 37
    monkeypatch.setattr(
        custom_lights,
        "_open_asset",
        lambda *_args, **_kwargs: Response(data, "https://attacker.invalid/asset.bin"),
    )
    with pytest.raises(ProtocolError, match="outside"):
        custom_lights.fetch_custom_light_asset(asset.name)
