"""Official QD35 custom-light catalog and verified asset loader."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import ProtocolError, TransportError


@dataclass(frozen=True, slots=True)
class CustomLightAsset:
    name: str
    asset_id: int
    light_type: int
    url: str
    sha256: str
    supports_palette: bool = False
    start_asset_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result.pop("url")
        return result


# Foreign/overseas QD35 catalog for product 223, firmware 1.3.0. The files are
# byte-identical to the corresponding China catalog assets, but IDs differ.
CUSTOM_LIGHTS = {
    "gradient": CustomLightAsset(
        "gradient",
        23,
        1,
        "https://edifier-us-app.s3.us-east-2.amazonaws.com/"
        "light_effect/20221206/abe3741aab008ef985558a22d876ddfc.bin",
        "76e9718b4f6685c55b98412e26711367204ec4eccd4a7b6e9a5faef203010a95",
    ),
    "water_drop": CustomLightAsset(
        "water_drop",
        24,
        1,
        "https://edifier-us-app.s3.us-east-2.amazonaws.com/"
        "light_effect/20221215/3c1d315d4e5fcb9d15010682bd162afe.bin",
        "0d5f4fba529ec4f383fdb31a61b8c99ddef12b3e1bbab54c96a5055ccbfb0782",
        supports_palette=True,
    ),
    "spiral": CustomLightAsset(
        "spiral",
        25,
        3,
        "https://edifier-us-app.s3.us-east-2.amazonaws.com/"
        "light_effect/20221206/9790202b33d3420dbc49f4068d3adab8.bin",
        "42c92f1a1f213f7732950bf8b89e5a6a8f26accb7e1edc865034d4fda002ea52",
        supports_palette=True,
        start_asset_id=1,
    ),
    "multicolored": CustomLightAsset(
        "multicolored",
        26,
        2,
        "https://edifier-us-app.s3.us-east-2.amazonaws.com/"
        "light_effect/20221215/dfbe7eec461ced2ca3cd6d1b648d880a.bin",
        "02eb0ac8c72b7d66434f0e14f9289dcebb83854d27cf0d7a675b472ed863bd06",
    ),
}

CUSTOM_LIGHT_PALETTES = {
    1: (255, 255, 255, 1),
    2: (125, 130, 255, 1),
    3: (67, 255, 240, 1),
    4: (255, 64, 219, 1),
    5: (113, 255, 86, 1),
    6: (255, 25, 0, 1),
    7: (253, 91, 0, 1),
    8: (255, 211, 0, 1),
    9: (70, 255, 0, 1),
    10: (0, 255, 122, 1),
    11: (230, 0, 255, 1),
}

_ASSET_HOSTS = frozenset({"edifier-us-app.s3.us-east-2.amazonaws.com"})


def _validate_asset_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ASSET_HOSTS:
        raise ProtocolError("custom-light download redirected outside the Edifier asset host")


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _validate_asset_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_asset(request: Request, timeout: float) -> Any:
    return build_opener(_AllowlistedRedirectHandler()).open(request, timeout=timeout)


def list_custom_lights() -> dict[str, object]:
    return {
        "assets": [asset.to_dict() for asset in CUSTOM_LIGHTS.values()],
        "palette_keys": sorted(CUSTOM_LIGHT_PALETTES),
        "source": "Edifier's product 223 / firmware 1.3.0 catalog",
    }


def fetch_custom_light_asset(
    name: str, palette_key: int | None = None
) -> tuple[CustomLightAsset, bytes]:
    if name not in CUSTOM_LIGHTS:
        raise ValueError(f"custom light must be one of: {', '.join(CUSTOM_LIGHTS)}")
    asset = CUSTOM_LIGHTS[name]
    if palette_key is not None:
        if not asset.supports_palette:
            raise ValueError(f"{name} does not support palette selection")
        if palette_key not in CUSTOM_LIGHT_PALETTES:
            raise ValueError("palette_key must be between 1 and 11")
    try:
        _validate_asset_url(asset.url)
        request = Request(asset.url, headers={"User-Agent": "qd35-mcp/0.1"})
        with _open_asset(request, timeout=20) as response:
            _validate_asset_url(response.geturl())
            data = response.read(1_000_001)
    except ProtocolError:
        raise
    except OSError as exc:
        raise TransportError(f"failed to download official custom-light asset: {exc}") from exc
    if len(data) > 1_000_000:
        raise ProtocolError("custom-light asset exceeds the 1 MB safety limit")
    digest = hashlib.sha256(data).hexdigest()
    if digest != asset.sha256:
        raise ProtocolError(
            f"custom-light asset hash mismatch: expected {asset.sha256}, received {digest}"
        )
    if not data or len(data) % 150:
        raise ProtocolError("custom-light asset is not composed of 150-byte animation records")
    if palette_key is not None:
        data = apply_palette(data, CUSTOM_LIGHT_PALETTES[palette_key])
    return asset, data


def apply_palette(data: bytes, palette: tuple[int, int, int, int]) -> bytes:
    result = bytearray(data)
    for record_start in range(0, len(result), 150):
        for component_start in range(record_start + 2, record_start + 150, 4):
            for component, multiplier in enumerate(palette):
                index = component_start + component
                result[index] = result[index] * multiplier // 255
    return bytes(result)
