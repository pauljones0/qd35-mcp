from __future__ import annotations

import asyncio
import re
from datetime import datetime
from functools import wraps
from typing import Any

from . import constants as c
from .config import Config
from .errors import (
    DangerousWritesDisabled,
    OutcomeUnknown,
    ProtocolError,
    QD35Error,
    WritesDisabled,
)
from .protocol import (
    parse_address,
    parse_eq,
    parse_eq_preset,
    parse_firmware,
    parse_input,
    parse_ldac,
    parse_light,
    parse_playback,
    parse_volume,
)
from .transport.base import Transport


def serialized_mutation(method: Any) -> Any:
    """Keep each semantic read/modify/write/readback transaction atomic."""

    @wraps(method)
    async def wrapped(self: QD35Service, *args: object, **kwargs: object) -> object:
        async with self._mutation_lock:
            return await method(self, *args, **kwargs)

    return wrapped


class QD35Service:
    def __init__(self, transport: Transport, config: Config) -> None:
        self.transport = transport
        self.config = config
        self._mutation_lock = asyncio.Lock()

    async def discover(self, timeout: float | None = None) -> dict[str, object]:
        timeout_value = self.config.scan_timeout if timeout is None else timeout
        if not 0 < timeout_value <= 300:
            raise ValueError("timeout must be greater than 0 and at most 300 seconds")
        devices = await self.transport.discover(timeout_value)
        return {"devices": [device.to_dict() for device in devices], "count": len(devices)}

    async def ensure_connected(self) -> dict[str, object]:
        device = await self.transport.connect(self.config.device)
        return device.to_dict()

    async def disconnect(self) -> dict[str, object]:
        await self.transport.disconnect()
        return {"connected": False}

    async def _request(self, opcode: int, payload: bytes = b"") -> bytes:
        if not self.transport.connected:
            await self.transport.connect(self.config.device)
        return await self.transport.request(opcode, payload, self.config.request_timeout)

    def _write_allowed(self, dangerous: bool = False) -> None:
        if not self.config.writes_enabled:
            raise WritesDisabled(
                "Writes are disabled; set QD35_ENABLE_WRITES=1 and restart the server"
            )
        if dangerous and not self.config.dangerous_writes_enabled:
            raise DangerousWritesDisabled(
                "This operation is disabled; set QD35_ENABLE_DANGEROUS=1 and restart the server"
            )

    async def status(self) -> dict[str, object]:
        device = await self.ensure_connected()

        async def read(opcode: int, parser: Any) -> object:
            try:
                return parser(await self._request(opcode))
            except QD35Error as exc:
                return {"unavailable": True, "error": str(exc)}

        volume = await read(c.QUERY_VOLUME, parse_volume)
        source = await read(c.QUERY_INPUT, parse_input)
        eq_preset = await read(c.QUERY_EQ_PRESET, parse_eq_preset)
        eq = await read(c.QUERY_EQ, parse_eq)
        lighting = await read(c.QUERY_LIGHT, parse_light)
        ldac = await read(c.QUERY_LDAC, parse_ldac)
        playback = await read(c.QUERY_PLAYBACK, parse_playback)
        firmware = await read(c.QUERY_FIRMWARE, parse_firmware)
        name = await read(c.QUERY_NAME, lambda value: value.decode("utf-8", "replace"))
        address = await read(c.QUERY_ADDRESS, parse_address)
        return {
            "device": device,
            "name": name,
            "firmware": firmware,
            "classic_address": address,
            "volume": volume,
            "input": source,
            "eq_preset": eq_preset,
            "custom_eq": eq,
            "lighting": lighting,
            "ldac": ldac,
            "playback": playback,
            "observed_at": datetime.now().astimezone().isoformat(),
        }

    async def capabilities(self) -> dict[str, object]:
        raw = await self._request(c.QUERY_CAPABILITIES)
        return {
            "model": "Edifier QD35",
            "raw_d8": raw.hex(),
            "observed_firmware": "1.3.0",
            "supported_tools": [
                "status",
                "volume",
                "input: bluetooth/aux/usb",
                "EQ presets and six DIY bands",
                "seven live-reported lighting effects, RGB, brightness and effect speed",
                "media play/pause/next/previous",
                "clock sync and 12/24-hour format",
                "LDAC off/48 kHz/96 kHz",
                "rename",
                "Bluetooth audio disconnect",
                "power off",
                "factory reset",
            ],
            "not_exposed": {
                "firmware_update": "OTA is intentionally excluded from an agent interface",
                "built_in_bgm": "The 32 audio files are app-local media, not speaker BLE commands",
                "remote_power_on": "Not supported by the official app",
                "raw_gatt": "Excluded to keep agents inside the QD35 semantic API",
            },
            "write_policy": {
                "writes_enabled": self.config.writes_enabled,
                "dangerous_writes_enabled": self.config.dangerous_writes_enabled,
            },
        }

    @serialized_mutation
    async def set_volume(self, level: int) -> dict[str, object]:
        self._write_allowed()
        current = parse_volume(await self._request(c.QUERY_VOLUME))
        if not 0 <= level <= current["maximum"]:
            raise ValueError(f"level must be between 0 and {current['maximum']}")
        await self._request(c.SET_VOLUME, bytes((level,)))
        result = parse_volume(await self._request(c.QUERY_VOLUME))
        return {"applied": result["current"] == level, "volume": result}

    @serialized_mutation
    async def set_input(self, source: str) -> dict[str, object]:
        self._write_allowed()
        source = source.strip().lower()
        if source not in c.INPUTS:
            raise ValueError(f"source must be one of: {', '.join(c.INPUTS)}")
        current = parse_input(await self._request(c.QUERY_INPUT))
        if current["group"] != 2:
            raise ProtocolError(f"expected QD35 input group 2, received {current['group']}")
        await self._request(c.SET_INPUT, bytes((2, c.INPUTS[source])))
        # Firmware 1.3.0 acknowledges immediately but applies the new source
        # asynchronously (observed at 0.75-1.25 seconds). Poll bounded readback
        # so a successful change is not reported as a false negative.
        result = current
        for attempt in range(9):
            result = parse_input(await self._request(c.QUERY_INPUT))
            if result["name"] == source:
                break
            if attempt < 8:
                await asyncio.sleep(0.25)
        return {"applied": result["name"] == source, "input": result}

    @serialized_mutation
    async def set_eq_preset(self, preset: str) -> dict[str, object]:
        self._write_allowed()
        preset = preset.strip().lower()
        if preset not in c.EQ_PRESETS:
            raise ValueError(f"preset must be one of: {', '.join(c.EQ_PRESETS)}")
        await self._request(c.SET_EQ_PRESET, bytes((c.EQ_PRESETS[preset],)))
        result = parse_eq_preset(await self._request(c.QUERY_EQ_PRESET))
        return {"applied": result["name"] == preset, "eq_preset": result}

    @serialized_mutation
    async def set_eq_band(self, band: int, gain_db: float) -> dict[str, object]:
        self._write_allowed()
        if not -3 <= gain_db <= 3 or round(gain_db * 2) != gain_db * 2:
            raise ValueError("gain_db must be from -3 to 3 in 0.5 dB steps")
        state = parse_eq(await self._request(c.QUERY_EQ))
        bands = state["bands"]
        if not 0 <= band < len(bands):
            raise ValueError(f"band must be between 0 and {len(bands) - 1}")
        record = bands[band]
        frequency = int(record["frequency_hz"])
        gain_code = int(gain_db * 2 + 6)
        payload = bytes((band, 0, frequency >> 8, frequency & 0xFF, gain_code, int(record["q"])))
        await self._request(c.SET_EQ, payload)
        await self._request(c.SET_EQ_PRESET, bytes((c.EQ_PRESETS["diy"],)))
        result = parse_eq(await self._request(c.QUERY_EQ))
        updated = next((item for item in result["bands"] if item["index"] == band), None)
        if updated is None:
            raise ProtocolError(f"EQ readback omitted band {band}")
        active_preset = parse_eq_preset(await self._request(c.QUERY_EQ_PRESET))
        band_applied = (
            updated["gain_db"] == gain_db
            and updated["frequency_hz"] == frequency
            and updated["q"] == record["q"]
        )
        return {
            "applied": band_applied and active_preset["name"] == "diy",
            "band": updated,
            "active_preset": active_preset,
        }

    @staticmethod
    def _rgb(value: str) -> tuple[int, int, int]:
        if not re.fullmatch(r"#?[0-9A-Fa-f]{6}", value):
            raise ValueError("rgb must be a six-digit hex color such as #FF8800")
        return tuple(bytes.fromhex(value.removeprefix("#")))  # type: ignore[return-value]

    @serialized_mutation
    async def set_light(
        self,
        effect: str | None = None,
        rgb: str | None = None,
        color_mode: str | None = None,
        brightness: int | None = None,
        speed: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, object]:
        self._write_allowed()
        state = parse_light(await self._request(c.QUERY_LIGHT))
        effect_id = state["selected_effect_id"]
        if effect is not None:
            effect = effect.strip().lower()
            if effect not in c.LIGHT_EFFECTS:
                raise ValueError(f"effect must be one of: {', '.join(c.LIGHT_EFFECTS)}")
            effect_id = c.LIGHT_EFFECTS[effect]
        records = {item["id"]: item for item in state["effects"]}
        if effect_id not in records:
            raise ProtocolError(f"effect {effect_id} is not reported by this firmware")
        current = records[effect_id]
        if rgb is not None and color_mode is not None:
            raise ValueError("rgb and color_mode cannot be used together")
        if effect_id in c.LIGHT_RGB_EFFECT_IDS:
            if color_mode is not None:
                raise ValueError(f"{current['name']} uses rgb, not color_mode")
            red, green, blue = tuple(current["raw_color"]) if rgb is None else self._rgb(rgb)
        else:
            if rgb is not None:
                allowed = ", ".join(c.LIGHT_COLOR_MODES[effect_id])
                raise ValueError(f"{current['name']} uses color_mode ({allowed}), not rgb")
            red, green, blue = current["raw_color"]
            if color_mode is not None:
                color_mode = color_mode.strip().lower()
                modes = c.LIGHT_COLOR_MODES[effect_id]
                if color_mode not in modes:
                    raise ValueError(
                        f"color_mode for {current['name']} must be one of: {', '.join(modes)}"
                    )
                red, green, blue = modes[color_mode], 0, 0
        target_brightness = current["brightness"] if brightness is None else brightness
        if enabled is True and brightness == 0:
            raise ValueError("enabled=true conflicts with brightness=0")
        if enabled is False and brightness not in (None, 0):
            raise ValueError("enabled=false conflicts with a nonzero brightness")
        if enabled is False:
            target_brightness = 0
        elif enabled is True and brightness is None and target_brightness == 0:
            target_brightness = 100
        if not 0 <= target_brightness <= 100:
            raise ValueError("brightness must be between 0 and 100")
        wire_speed = current["wire_speed"]
        if speed is not None:
            if not 1 <= speed <= 10:
                raise ValueError("speed must be between 1 and 10")
            if wire_speed == 0xFF:
                raise ValueError(f"{current['name']} does not support speed")
            wire_speed = 11 - speed
        payload = bytes(
            (state["group"], effect_id, red, green, blue, target_brightness, wire_speed)
        )
        await self._request(c.SET_LIGHT, payload)
        result = parse_light(await self._request(c.QUERY_LIGHT))
        selected = next((item for item in result["effects"] if item["id"] == effect_id), None)
        if selected is None:
            raise ProtocolError(f"lighting readback omitted requested effect {effect_id}")
        applied = (
            result["group"] == state["group"]
            and result["selected_effect_id"] == effect_id
            and selected["raw_color"] == [red, green, blue]
            and all(item["brightness"] == target_brightness for item in result["effects"])
            and selected["wire_speed"] == wire_speed
        )
        return {"applied": applied, "lighting": result}

    @serialized_mutation
    async def media_control(self, action: str) -> dict[str, object]:
        self._write_allowed()
        action = action.strip().lower()
        if action not in c.MEDIA_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(c.MEDIA_ACTIONS)}")
        before = parse_playback(await self._request(c.QUERY_PLAYBACK))
        await self.transport.send(c.MEDIA_CONTROL, bytes((c.MEDIA_ACTIONS[action],)))
        await asyncio.sleep(0.1)
        try:
            after = parse_playback(await self._request(c.QUERY_PLAYBACK))
        except QD35Error as exc:
            return {
                "sent": True,
                "action": action,
                "applied": None,
                "playback_before": before,
                "playback_after": {"unavailable": True, "error": str(exc)},
                "verification": "command sent; playback readback failed",
            }
        applied = None
        if action == "play":
            if after["playing"] is False:
                applied = False
            elif before["playing"] is False:
                applied = True
        elif action == "pause":
            if after["playing"] is True:
                applied = False
            elif before["playing"] is True:
                applied = True
        return {
            "sent": True,
            "action": action,
            "applied": applied,
            "acknowledgement": None,
            "playback_before": before,
            "playback_after": after,
            "changed": before["playing"] != after["playing"],
            "verification": (
                "C2 is fire-and-forget; C3 independently reports playing state but cannot "
                "verify track changes"
            ),
        }

    @serialized_mutation
    async def sync_clock(self, clock_format: str = "preserve") -> dict[str, object]:
        self._write_allowed()
        clock_format = clock_format.strip().lower()
        if clock_format not in {"preserve", "12h", "24h"}:
            raise ValueError("clock_format must be preserve, 12h, or 24h")
        for attempt in range(1, 3):
            now = datetime.now().astimezone()
            if clock_format == "preserve":
                payload = bytes((now.hour, now.minute, now.second, 0))
            elif clock_format == "12h":
                hour = now.hour % 12 or 12
                payload = bytes((hour, now.minute, now.second, 1, 1 if now.hour >= 12 else 0))
            else:
                payload = bytes((now.hour, now.minute, now.second, 2, 1 if now.hour >= 12 else 0))
            response = await self._request(c.SYNC_CLOCK, payload)
            if response == b"\x01":
                return {
                    "sent": True,
                    "acknowledged": True,
                    "applied": None,
                    "time": now.isoformat(),
                    "format": clock_format,
                    "response": response.hex(),
                    "attempts": attempt,
                    "verification": "speaker returned success; this protocol has no clock readback",
                }
            if response != b"\x02":
                raise ProtocolError(
                    f"clock sync returned unexpected response {response.hex() or '<empty>'}"
                )
        raise ProtocolError("speaker requested clock resynchronization twice")

    @serialized_mutation
    async def set_ldac(self, mode: str) -> dict[str, object]:
        self._write_allowed()
        mode = mode.strip().lower()
        if mode not in c.LDAC_MODES:
            raise ValueError(f"mode must be one of: {', '.join(c.LDAC_MODES)}")
        expected = c.LDAC_MODES[mode]
        before = parse_ldac(await self._request(c.QUERY_LDAC))
        if before["value"] == expected:
            return {
                "applied": True,
                "changed": False,
                "bluetooth_restarted": False,
                "ldac": before,
            }
        try:
            await self._request(c.SET_LDAC, bytes((expected,)))
            # Some firmware acknowledges before its Bluetooth stack restarts.
            await asyncio.sleep(0.25)
            result = parse_ldac(await self._request(c.QUERY_LDAC))
            return {"applied": result["value"] == expected, "changed": True, "ldac": result}
        except QD35Error:
            await self.transport.disconnect()
            return {
                "applied": None,
                "changed": True,
                "bluetooth_restarted": True,
                "requested_mode": mode,
                "outcome": "unknown_until_reconnected",
                "suggested_action": (
                    "Reconnect Classic audio, then call qd35_get_status to verify LDAC. "
                    "If the old bond is rejected, remove it from the host and pair again."
                ),
            }

    @serialized_mutation
    async def rename(self, name: str) -> dict[str, object]:
        self._write_allowed()
        normalized = name.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("name must not contain control characters")
        encoded = normalized.encode("utf-8")
        if not encoded or len(encoded) > 24:
            raise ValueError("name must be 1 to 24 UTF-8 bytes")
        await self._request(c.SET_NAME, encoded)
        result = (await self._request(c.QUERY_NAME)).decode("utf-8", "replace")
        return {"applied": result == normalized, "name": result}

    async def _disruptive(self, opcode: int, operation: str) -> dict[str, object]:
        self._write_allowed(dangerous=True)
        try:
            response = await self._request(opcode)
            if response != b"\x01":
                raise ProtocolError(
                    f"{operation} returned unexpected response {response.hex() or '<empty>'}"
                )
            return {"sent": True, "operation": operation, "response": response.hex()}
        except OutcomeUnknown:
            return {
                "sent": True,
                "operation": operation,
                "connection_dropped": True,
                "outcome": "expected_disconnect; verify physically or rediscover",
            }

    @serialized_mutation
    async def power_off(self) -> dict[str, object]:
        return await self._disruptive(c.POWER_OFF, "power_off")

    @serialized_mutation
    async def disconnect_audio(self) -> dict[str, object]:
        return await self._disruptive(c.DISCONNECT_AUDIO, "disconnect_audio")

    @serialized_mutation
    async def factory_reset(self) -> dict[str, object]:
        return await self._disruptive(c.FACTORY_RESET, "factory_reset")
