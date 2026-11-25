from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import constants as c
from .animations import (
    DIAGNOSTIC_ANIMATION_NAMES,
    GeneratedAnimation,
    build_creative_animation,
    build_diagnostic_animation,
    compile_authored_animation,
    compile_reactive_animation,
    list_creative_animations,
    list_diagnostic_animations,
)
from .config import Config
from .custom_lights import CustomLightAsset, fetch_custom_light_asset, list_custom_lights
from .display import (
    DISPLAY_CLOCK,
    DISPLAY_DEFAULT_FLAGS,
    DISPLAY_FOLLOW_DIAL,
    DISPLAY_LOGO,
    DISPLAY_POWER,
    DISPLAY_RAW_MODE,
    DISPLAY_SYMBOLS,
    DISPLAY_UPDATE_BRIGHTNESS,
    describe_flags,
    grid_payload,
    reset_payload,
    state_payload,
    state_update_payload,
)
from .errors import (
    DangerousWritesDisabled,
    DeviceNotFound,
    OutcomeUnknown,
    ProtocolError,
    QD35Error,
    RequestTimeout,
    WritesDisabled,
)
from .firmware_patch import (
    PATCH_ADDRESS,
    PATCH_CODE,
    PATCH_LIMIT,
    PATCHED_SHA256,
    STOCK_SHA256,
    STOCK_SIZE,
    STOCK_URL,
    download_stock_image,
    patch_image,
    sha256,
)
from .ownership import SpeakerOwnership
from .protocol import (
    parse_address,
    parse_audio_status,
    parse_capabilities,
    parse_custom_light_finish,
    parse_custom_light_progress,
    parse_custom_light_start,
    parse_custom_light_status,
    parse_eq,
    parse_eq_preset,
    parse_firmware,
    parse_input,
    parse_ldac,
    parse_light,
    parse_light_setting,
    parse_light_timer,
    parse_media_info,
    parse_playback,
    parse_text_event,
    parse_volume,
)
from .timezones import (
    default_timezone_state_path,
    persist_timezone,
    resolve_timezone,
    validate_timezone,
)
from .transport.base import Transport


def serialized_mutation(method: Any) -> Any:
    """Keep each semantic read/modify/write/readback transaction atomic."""

    @wraps(method)
    async def wrapped(self: QD35Service, *args: object, **kwargs: object) -> object:
        async with self._transaction():
            return await method(self, *args, **kwargs)

    return wrapped


class QD35Service:
    def __init__(
        self, transport: Transport, config: Config, ownership: SpeakerOwnership | None = None
    ) -> None:
        self.transport = transport
        self.config = config
        self._mutation_lock = asyncio.Lock()
        self._ownership = ownership
        self._ble_locks: AsyncExitStack | None = None
        self._locked_address: str | None = None
        self._connected_device: dict[str, object] | None = None
        self._verified_identity: tuple[str, int] | None = None
        self._transaction_active = False
        self._transaction_identity: tuple[str, int] | None = None
        self._transaction_mutated = False
        self._timezone_state_path = (
            Path(config.timezone_state_path).expanduser()
            if config.timezone_state_path
            else default_timezone_state_path()
        )
        self._timezone = resolve_timezone(config.timezone, self._timezone_state_path)
        self._display_flags = DISPLAY_DEFAULT_FLAGS
        self._display_brightness = 7
        self._display_state_known = False
        self._display_grid: list[int | None] = [None] * 8
        self._display_session_identity: tuple[str, int] | None = None

    @asynccontextmanager
    async def _transaction(self):
        async with self._mutation_lock, AsyncExitStack() as locks:
            self._ble_locks = locks
            self._transaction_active = True
            try:
                yield
            finally:
                self._transaction_active = False
                self._transaction_identity = None
                self._transaction_mutated = False
                self._locked_address = None
                self._ble_locks = None

    async def _lock_ble_target(self) -> str | None:
        identity = self.transport.connection_identity
        if self._ownership is None:
            return self.config.device
        if self._locked_address is not None:
            return self._locked_address
        address = self.config.device or (identity[0] if identity else None)
        if address is None:
            candidates = await self.transport.discover(self.config.scan_timeout)
            if not candidates:
                raise DeviceNotFound("No QD35 advertising the a400 service was found")
            if len(candidates) != 1:
                addresses = ", ".join(candidate.address for candidate in candidates)
                raise DeviceNotFound(
                    f"Multiple QD35 candidates found; set QD35_DEVICE: {addresses}"
                )
            address = candidates[0].address
        assert self._ble_locks is not None
        await self._ble_locks.enter_async_context(self._ownership.lock("ble", address))
        self._locked_address = address
        return address

    async def discover(self, timeout: float | None = None) -> dict[str, object]:
        timeout_value = self.config.scan_timeout if timeout is None else timeout
        if not 0 < timeout_value <= 300:
            raise ValueError("timeout must be greater than 0 and at most 300 seconds")
        async with self._mutation_lock:
            devices = await self.transport.discover(timeout_value)
        return {"devices": [device.to_dict() for device in devices], "count": len(devices)}

    async def ensure_connected(self) -> dict[str, object]:
        async with self._transaction():
            return await self._ensure_connected()

    async def _ensure_connected(self) -> dict[str, object]:
        address = await self._lock_ble_target()
        device = await self.transport.connect(address)
        locked = self._locked_address
        if locked is not None and device.address.upper() != locked.upper():
            raise ProtocolError("Bluetooth transport connected to an unlocked speaker")
        result = device.to_dict()
        if self._verified_identity != self.transport.connection_identity:
            self._verified_identity = None
        if (
            self._display_session_identity is not None
            and self._display_session_identity != self.transport.connection_identity
        ):
            self._display_state_known = False
        self._connected_device = result
        return result

    async def disconnect(self) -> dict[str, object]:
        async with self._transaction():
            if self.transport.connected:
                await self._lock_ble_target()
            await self.transport.disconnect()
        return {"connected": False}

    async def _request(self, opcode: int, payload: bytes = b"") -> bytes:
        if not self.transport.connected:
            if self._transaction_active and self._transaction_identity is not None:
                error = (
                    OutcomeUnknown
                    if self._transaction_mutated
                    else ProtocolError
                )
                raise error(
                    "Bluetooth session changed during one semantic transaction; "
                    "refusing to reconnect and continue on a possibly different speaker"
                )
            await self._ensure_connected()
        else:
            await self._lock_ble_target()
        identity = self.transport.connection_identity
        if identity is None:
            raise ProtocolError("Bluetooth transport has no active connection identity")
        if self._locked_address is not None and identity[0].upper() != self._locked_address.upper():
            raise ProtocolError("Bluetooth transport changed its locked speaker")
        if self._transaction_active:
            if self._transaction_identity is None:
                self._transaction_identity = identity
            elif identity != self._transaction_identity:
                error = OutcomeUnknown if self._transaction_mutated else ProtocolError
                raise error(
                    "Bluetooth target/session changed during one semantic transaction; "
                    "refusing to continue"
                )
        if opcode in c.MUTATING_OPCODES and self._verified_identity != identity:
            await self._verify_qd35_target()
            if self.transport.connection_identity != identity:
                raise ProtocolError("Bluetooth session changed while verifying the QD35 target")
        mutating = opcode in c.MUTATING_OPCODES and self._transaction_active
        try:
            response = await self.transport.request(opcode, payload, self.config.request_timeout)
        except OutcomeUnknown:
            self._transaction_mutated |= mutating
            raise
        self._transaction_mutated |= mutating
        return response

    async def _verify_qd35_target(self) -> dict[str, object]:
        """Establish model-specific evidence before the first physical write."""
        identity = self.transport.connection_identity
        if self._verified_identity is not None and self._verified_identity == identity:
            return {"verified": True, "address": identity[0], "cached": True}
        if not self.transport.connected:
            await self._ensure_connected()
        identity = self.transport.connection_identity
        if identity is None:
            raise ProtocolError("Bluetooth transport has no active connection identity")
        name = (await self.transport.request(
            c.QUERY_NAME, timeout=self.config.request_timeout
        )).decode("utf-8", "replace")
        capabilities = parse_capabilities(
            await self.transport.request(c.QUERY_CAPABILITIES, timeout=self.config.request_timeout)
        )
        evidence: dict[str, object] = {"name": name, "capabilities": capabilities}
        source = parse_input(
            await self.transport.request(c.QUERY_INPUT, timeout=self.config.request_timeout)
        )
        volume = parse_volume(
            await self.transport.request(c.QUERY_VOLUME, timeout=self.config.request_timeout)
        )
        lighting = parse_light(
            await self.transport.request(c.QUERY_LIGHT, timeout=self.config.request_timeout)
        )
        evidence.update(
            {"input": source, "volume": volume, "lighting_group": lighting["group"]}
        )
        fingerprint_match = (
            capabilities["eq_profile"] == 12
            and capabilities["skin_profile"] == 10
            and source["group"] == 2
            and volume["maximum"] == 16
            and lighting["group"] == 1
            and any(record["id"] == 7 for record in lighting["effects"])
        )
        if not fingerprint_match:
            raise ProtocolError(
                "The selected Edifier A400 device could not be verified as a QD35; "
                "refusing to write"
            )
        if self.transport.connection_identity != identity:
            raise ProtocolError("Bluetooth session changed while verifying the QD35 target")
        self._verified_identity = identity
        return {"verified": True, "address": identity[0], "evidence": evidence}

    def _write_allowed(self, dangerous: bool = False) -> None:
        if not self.config.writes_enabled:
            raise WritesDisabled(
                "Writes are disabled; set QD35_ENABLE_WRITES=1 and restart the server"
            )
        if dangerous and not self.config.dangerous_writes_enabled:
            raise DangerousWritesDisabled(
                "This operation is disabled; set QD35_ENABLE_DANGEROUS=1 and restart the server"
            )

    def _display_write_allowed(self, dangerous: bool = False) -> None:
        self._write_allowed(dangerous=dangerous)
        if not self.config.custom_display_firmware:
            raise ProtocolError(
                "Display controls require the exact qd35-mcp MCU patch and "
                "QD35_CUSTOM_DISPLAY_FIRMWARE=1; stock firmware does not implement them"
            )

    def _firmware_install_allowed(self, require_patch_enabled: bool) -> None:
        self._write_allowed(dangerous=True)
        if require_patch_enabled and not self.config.custom_display_firmware:
            raise ProtocolError(
                "Set QD35_CUSTOM_DISPLAY_FIRMWARE=1 to install the display patch"
            )
        if not self.config.firmware_install_enabled:
            raise ProtocolError(
                "Firmware installation is disabled; set QD35_ENABLE_FIRMWARE_INSTALL=1"
            )

    async def status(self) -> dict[str, object]:
        async with self._transaction():
            return await self._status_locked()

    @property
    def alarm_control_connected(self) -> bool:
        return self.transport.connected

    async def reconnect_alarm_controls(self) -> dict[str, object]:
        async with self._transaction():
            await self._request(c.QUERY_CAPABILITIES)
            return parse_volume(await self._request(c.QUERY_VOLUME))

    async def alarm_control(self, field: str) -> dict[str, object]:
        """Read one side-control setting without running the full status sweep."""
        opcode, parser = {
            "volume": (c.QUERY_VOLUME, parse_volume),
            "input": (c.QUERY_INPUT, parse_input),
            "eq_preset": (c.QUERY_EQ_PRESET, parse_eq_preset),
            "lighting": (c.QUERY_LIGHT, parse_light),
        }[field]
        async with self._transaction():
            return parser(await self._request(opcode))

    async def _status_locked(self) -> dict[str, object]:
        device = await self._ensure_connected()
        self._transaction_identity = self.transport.connection_identity

        async def read(opcode: int, parser: Any) -> object:
            try:
                return parser(await self._request(opcode))
            except QD35Error as exc:
                return {"unavailable": True, "error": str(exc)}

        capabilities = await read(c.QUERY_CAPABILITIES, parse_capabilities)
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
        audio_status = await read(c.QUERY_AUDIO_STATUS, parse_audio_status)
        custom_light = await read(c.QUERY_CUSTOM_LIGHT, parse_custom_light_status)
        light_timer: object = {
            "unavailable": True,
            "unsupported": True,
            "error": "D8 does not advertise ambient-light timer support",
        }
        if (
            isinstance(capabilities, dict)
            and isinstance(capabilities.get("supported"), dict)
            and capabilities["supported"].get("light_timer")
        ):
            light_timer = await read(c.QUERY_LIGHT_TIMER, parse_light_timer)
        return {
            "device": device,
            "capabilities": capabilities,
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
            "audio_status": audio_status,
            "custom_light": custom_light,
            "light_timer": light_timer,
            "timezone": self._timezone,
            "observed_at": datetime.now().astimezone().isoformat(),
        }

    async def capabilities(self) -> dict[str, object]:
        async with self._transaction():
            raw = await self._request(c.QUERY_CAPABILITIES)
            decoded = parse_capabilities(raw)
        return {
            "model": "Edifier QD35",
            "device_capabilities": decoded,
            "supported_tools": [
                "status",
                "volume",
                "input: bluetooth/aux/usb",
                "EQ presets and six DIY bands",
                "seven live-reported lighting effects, RGB, brightness and effect speed",
                "lighting reset and capability-gated light timer",
                "official Extended/custom-light animation transfer",
                "audio decoding status and custom-light status",
                "retained raw and parsed notification events",
                "media play/pause/next/previous",
                "clock sync and 12/24-hour format",
                "LDAC off/48 kHz/96 kHz",
                "rename",
                "Bluetooth audio disconnect",
                "power off",
                "factory reset",
                *(
                    [
                        "display power, clock digits, clock-area line, and status symbol masks",
                        "whole-display off extinguishes the physical wordmark",
                        "display brightness 0-7 or follow the right-side lighting dial",
                        "raw access to the eight display RAM bytes used by QD35 firmware",
                    ]
                    if self.config.custom_display_firmware
                    else []
                ),
                *(
                    [
                        "install the bundled display patch from the exact pinned Edifier image",
                        "restore the exact pinned stock firmware image",
                    ]
                    if self.config.firmware_install_enabled
                    else []
                ),
            ],
            "not_exposed": {
                **(
                    {}
                    if self.config.firmware_install_enabled
                    else {
                        "firmware_install": (
                            "The fixed display-patch and stock-restore MCP tools require "
                            "QD35_ENABLE_FIRMWARE_INSTALL=1 plus both write gates"
                        )
                    }
                ),
                "firmware_update": "The server does not check Edifier for newer firmware",
                "built_in_bgm": "The 32 audio files are app-local media, not speaker BLE commands",
                "remote_power_on": "Not supported by the official app",
                **(
                    {}
                    if self.config.custom_display_firmware
                    else {
                        "custom_display": (
                            "Stock firmware lacks custom display controls. Build and "
                            "install the exact-hash MCU patch, then set "
                            "QD35_CUSTOM_DISPLAY_FIRMWARE=1."
                        )
                    }
                ),
                "raw_gatt": "Excluded to keep agents inside the QD35 semantic API",
            },
            "write_policy": {
                "writes_enabled": self.config.writes_enabled,
                "dangerous_writes_enabled": self.config.dangerous_writes_enabled,
                "custom_display_firmware": self.config.custom_display_firmware,
                "firmware_install_enabled": self.config.firmware_install_enabled,
            },
        }

    async def display_status(self) -> dict[str, object]:
        """Return the last requested display state and whether the command was echoed."""
        current_session = self.transport.connection_identity
        matching_session = (
            current_session is not None and current_session == self._display_session_identity
        )
        return {
            "available": self.config.custom_display_firmware,
            "state_known_this_process": self._display_state_known and matching_session,
            "cached_session_matches": matching_session,
            "state": describe_flags(self._display_flags, self._display_brightness),
            "grid_bytes": self._display_grid.copy(),
            "device_readback": False,
            "volatile": True,
            "note": (
                "The patch resets to display on, clock/logo/symbols on, brightness 7 after a "
                "speaker power cycle. Values here are locally retained requests; the "
                "ordinary lighting reply does not confirm the visual result."
            ),
        }

    async def _display_request(self, payload: bytes) -> tuple[bytes, bool]:
        response = await self._request(c.SET_LIGHT, payload)
        echoed = response == payload
        if not echoed:
            # The installed patch reports ordinary lighting state on tested hardware.
            # Reject malformed responses, but don't mistake this for a private echo.
            try:
                parse_light_setting(response)
            except ProtocolError as exc:
                raise OutcomeUnknown("display write returned an invalid lighting reply") from exc
        return response, echoed

    async def display_firmware_info(self) -> dict[str, object]:
        """Describe the bundled original patch and its fixed install contract."""
        return {
            "patch_bundled": True,
            "patch_bytes": len(PATCH_CODE),
            "patch_address": f"0x{PATCH_ADDRESS:08X}",
            "animation_storage_starts": f"0x{PATCH_LIMIT:08X}",
            "official_source_url": STOCK_URL,
            "official_source_size": STOCK_SIZE,
            "official_source_sha256": STOCK_SHA256,
            "patched_image_sha256": PATCHED_SHA256,
            "custom_display_firmware_enabled": self.config.custom_display_firmware,
            "firmware_install_enabled": self.config.firmware_install_enabled,
            "accepts_arbitrary_images": False,
            "install_confirmation": "INSTALL DISPLAY PATCH",
            "restore_confirmation": "RESTORE STOCK FIRMWARE",
        }

    @serialized_mutation
    async def set_display(
        self,
        power: bool | None = None,
        clock: bool | None = None,
        logo: bool | None = None,
        symbols: bool | None = None,
        brightness: int | None = None,
        follow_lighting_dial: bool | None = None,
        raw_mode: bool | None = None,
    ) -> dict[str, object]:
        """Set patched display controls. The logo argument masks the clock-area line."""
        self._display_write_allowed(dangerous=raw_mode is True)
        if brightness is not None and not 0 <= brightness <= 7:
            raise ValueError("brightness must be from 0 to 7")
        updates = {
            DISPLAY_POWER: power,
            DISPLAY_CLOCK: clock,
            DISPLAY_LOGO: logo,
            DISPLAY_SYMBOLS: symbols,
            DISPLAY_FOLLOW_DIAL: follow_lighting_dial,
            DISPLAY_RAW_MODE: raw_mode,
        }
        if all(value is None for value in updates.values()) and brightness is None:
            raise ValueError("provide at least one display setting to change")
        flags = self._display_flags
        mask = 0
        for bit, value in updates.items():
            if value is not None:
                mask |= bit
            if value is True:
                flags |= bit
            elif value is False:
                flags &= ~bit
        level = self._display_brightness if brightness is None else brightness
        if brightness is not None:
            mask |= DISPLAY_UPDATE_BRIGHTNESS
        complete = mask == 0x7F
        payload = state_payload(flags, level) if complete else state_update_payload(
            flags & mask, brightness if brightness is not None else 0, mask
        )
        response, echoed = await self._display_request(payload)
        self._display_flags = flags
        self._display_brightness = level
        self._display_state_known = echoed and complete
        self._display_session_identity = self.transport.connection_identity
        requested = {
            key: value for key, value in {
                "power": power, "clock": clock, "logo": logo, "symbols": symbols,
                "brightness": brightness, "follow_lighting_dial": follow_lighting_dial,
                "raw_mode": raw_mode,
            }.items() if value is not None
        }
        if logo is not None:
            requested["line"] = logo
        return {
            "acknowledged": True,
            "applied": None,
            "state": describe_flags(flags, level) if complete else requested,
            "state_scope": "complete_request" if complete else "partial_request",
            "acknowledgement_hex": response.hex(),
            "reply_confirms_command": echoed,
            "device_readback": False,
        }

    @serialized_mutation
    async def write_display_grid_byte(self, index: int, value: int) -> dict[str, object]:
        """Write one raw CMS1640 grid byte; raw mode is controlled separately."""
        self._display_write_allowed(dangerous=True)
        response, echoed = await self._display_request(grid_payload(index, value))
        self._display_grid[index] = value if echoed else None
        self._display_session_identity = self.transport.connection_identity
        return {
            "acknowledged": True,
            "applied": None,
            "index": index,
            "value": value,
            "value_hex": f"0x{value:02X}",
            "raw_mode": bool(self._display_flags & DISPLAY_RAW_MODE),
            "acknowledgement_hex": response.hex(),
            "reply_confirms_command": echoed,
            "device_readback": False,
        }

    @serialized_mutation
    async def reset_display(self) -> dict[str, object]:
        """Restore the patch's safe stock-equivalent volatile defaults."""
        self._display_write_allowed()
        response, echoed = await self._display_request(reset_payload())
        self._display_flags = DISPLAY_DEFAULT_FLAGS
        self._display_brightness = 7
        self._display_state_known = echoed
        self._display_grid = [None] * 8
        self._display_session_identity = self.transport.connection_identity
        return {
            "acknowledged": True,
            "applied": None,
            "state": describe_flags(self._display_flags, self._display_brightness),
            "acknowledgement_hex": response.hex(),
            "reply_confirms_command": echoed,
            "device_readback": False,
        }

    @serialized_mutation
    async def install_display_firmware(self, stock_image: bytes) -> dict[str, object]:
        """Install the exact-hash display patch using the supplied stock image."""
        self._firmware_install_allowed(require_patch_enabled=True)
        if not self.config.device:
            raise ProtocolError(
                "Firmware installation requires an explicit QD35_DEVICE address"
            )
        patched = patch_image(stock_image)
        await self._ensure_connected()
        verified = await self._verify_qd35_target()
        identity = self.transport.connection_identity
        if identity is None or identity[0].lower() != self.config.device.lower():
            raise ProtocolError("connected firmware target does not match QD35_DEVICE")
        current = parse_firmware(await self._request(c.QUERY_FIRMWARE))
        if current != "1.3.0":
            raise ProtocolError(
                f"display patch supports QD35 firmware 1.3.0, found {current}"
            )
        self._transaction_mutated = True
        transfer = await self.transport.ota_update(patched, (1, 3, 0))
        return {
            "target": verified,
            "stock_sha256": STOCK_SHA256,
            "patched_sha256": PATCHED_SHA256,
            "transmitted_sha256": PATCHED_SHA256,
            "transfer": transfer,
            "verification_required": True,
            "next_step": (
                "Wait for the lighting MCU to restart, reconnect, then exercise "
                "qd35_set_display. The OTA protocol cannot read back an installed image hash."
            ),
        }

    @serialized_mutation
    async def restore_stock_firmware(self, stock_image: bytes) -> dict[str, object]:
        """Send the exact supplied official image for stock restoration."""
        self._firmware_install_allowed(require_patch_enabled=False)
        if len(stock_image) != STOCK_SIZE or sha256(stock_image) != STOCK_SHA256:
            raise ValueError("restore requires the exact supported official QD35 1.3.0 image")
        if not self.config.device:
            raise ProtocolError("Firmware restoration requires an explicit QD35_DEVICE address")
        await self._ensure_connected()
        verified = await self._verify_qd35_target()
        identity = self.transport.connection_identity
        if identity is None or identity[0].lower() != self.config.device.lower():
            raise ProtocolError("connected firmware target does not match QD35_DEVICE")
        current = parse_firmware(await self._request(c.QUERY_FIRMWARE))
        if current != "1.3.0":
            raise ProtocolError(
                f"stock restore supports QD35 firmware lineage 1.3.0, found {current}"
            )
        self._transaction_mutated = True
        transfer = await self.transport.ota_update(stock_image, (1, 3, 0))
        return {
            "target": verified,
            "transmitted_sha256": STOCK_SHA256,
            "transfer": transfer,
            "verification_required": True,
        }

    async def install_display_firmware_from_edifier(self) -> dict[str, object]:
        """Fetch the one pinned official source image and install the bundled patch."""
        self._firmware_install_allowed(require_patch_enabled=True)
        stock = await download_stock_image()
        return await self.install_display_firmware(stock)

    async def restore_stock_firmware_from_edifier(self) -> dict[str, object]:
        """Fetch and restore the one pinned official source image."""
        self._firmware_install_allowed(require_patch_enabled=False)
        stock = await download_stock_image()
        return await self.restore_stock_firmware(stock)

    async def events(self, since_sequence: int = 0, limit: int = 100) -> dict[str, object]:
        result = await self.transport.events(since_sequence, limit)
        parsed_events: list[dict[str, object]] = []
        parsers: dict[int, Any] = {
            c.QUERY_VOLUME: parse_volume,
            c.QUERY_INPUT: parse_input,
            c.QUERY_EQ_PRESET: parse_eq_preset,
            c.QUERY_LIGHT: parse_light,
            c.SET_LIGHT: parse_light_setting,
            c.RESET_LIGHT: parse_light,
            c.QUERY_PLAYBACK: parse_playback,
            c.MEDIA_INFO: parse_media_info,
            c.MEDIA_TITLE_EVENT: parse_text_event,
            c.MEDIA_ARTIST_EVENT: parse_text_event,
            c.QUERY_AUDIO_STATUS: parse_audio_status,
            c.QUERY_CUSTOM_LIGHT: parse_custom_light_status,
            c.QUERY_LIGHT_TIMER: parse_light_timer,
        }
        for event in result["events"]:
            item = dict(event)
            opcode = item.get("opcode")
            payload_hex = item.get("payload_hex")
            if isinstance(opcode, int) and opcode in parsers and isinstance(payload_hex, str):
                try:
                    item["parsed"] = parsers[opcode](bytes.fromhex(payload_hex))
                except QD35Error as exc:
                    item["parse_error"] = str(exc)
            parsed_events.append(item)
        return {**result, "events": parsed_events}

    async def custom_light_catalog(self) -> dict[str, object]:
        return {
            "official": list_custom_lights(),
            "creative": list_creative_animations(),
            "diagnostic": list_diagnostic_animations(),
        }

    @serialized_mutation
    async def set_volume(self, level: int) -> dict[str, object]:
        self._write_allowed()
        current = parse_volume(await self._request(c.QUERY_VOLUME))
        if not 0 <= level <= current["maximum"]:
            raise ValueError(f"level must be between 0 and {current['maximum']}")
        await self._request(c.SET_VOLUME, bytes((level,)))
        try:
            result = parse_volume(await self._request(c.QUERY_VOLUME))
        except QD35Error as exc:
            return self._unknown_after_write("set_volume", exc, ["volume_write"])
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
        if current["name"] == source:
            return {"applied": True, "changed": False, "input": current}
        await self._request(c.SET_INPUT, bytes((2, c.INPUTS[source])))
        # Firmware 1.3.0 acknowledges immediately but applies the new source
        # asynchronously (observed at 0.75-1.25 seconds). Poll bounded readback
        # so a successful change is not reported as a false negative.
        result = current
        try:
            for attempt in range(9):
                result = parse_input(await self._request(c.QUERY_INPUT))
                if result["name"] == source:
                    break
                if attempt < 8:
                    await asyncio.sleep(0.25)
        except QD35Error as exc:
            return self._unknown_after_write("set_input", exc, ["input_write"])
        return {
            "applied": result["name"] == source,
            "changed": result["name"] == source,
            "input": result,
        }

    @serialized_mutation
    async def set_eq_preset(self, preset: str) -> dict[str, object]:
        self._write_allowed()
        preset = preset.strip().lower()
        if preset not in c.EQ_PRESETS:
            raise ValueError(f"preset must be one of: {', '.join(c.EQ_PRESETS)}")
        await self._request(c.SET_EQ_PRESET, bytes((c.EQ_PRESETS[preset],)))
        try:
            result = parse_eq_preset(await self._request(c.QUERY_EQ_PRESET))
        except QD35Error as exc:
            return self._unknown_after_write("set_eq_preset", exc, ["preset_write"])
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
        completed_steps = ["band_write"]
        try:
            await self._request(c.SET_EQ_PRESET, bytes((c.EQ_PRESETS["diy"],)))
            completed_steps.append("diy_preset_write")
            result = parse_eq(await self._request(c.QUERY_EQ))
            updated = next((item for item in result["bands"] if item["index"] == band), None)
            if updated is None:
                raise ProtocolError(f"EQ readback omitted band {band}")
            active_preset = parse_eq_preset(await self._request(c.QUERY_EQ_PRESET))
        except QD35Error as exc:
            return self._unknown_after_write("set_eq_band", exc, completed_steps)
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

    @serialized_mutation
    async def reset_eq(self) -> dict[str, object]:
        self._write_allowed()
        before = parse_eq(await self._request(c.QUERY_EQ))
        response = await self._request(c.RESET_EQ)
        try:
            after = parse_eq(await self._request(c.QUERY_EQ))
        except QD35Error as exc:
            return self._unknown_after_write("reset_eq", exc, ["reset_write"])
        expected_band_count = before["band_count"]
        reset_applied = (
            expected_band_count == 6
            and after["band_count"] == expected_band_count
            and len(after["bands"]) == expected_band_count
            and all(band["gain_db"] == 0 for band in after["bands"])
        )
        return {
            "sent": True,
            "acknowledgement_hex": response.hex(),
            "applied": reset_applied,
            "changed": before != after,
            "custom_eq": after,
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
            if effect_id not in c.LIGHT_COLOR_MODES:
                raise ProtocolError(
                    f"firmware selected unknown lighting effect {effect_id}; only raw state "
                    "can be preserved safely"
                )
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
        try:
            result = parse_light(await self._request(c.QUERY_LIGHT))
            selected = next(
                (item for item in result["effects"] if item["id"] == effect_id), None
            )
            if selected is None:
                raise ProtocolError(f"lighting readback omitted requested effect {effect_id}")
        except QD35Error as exc:
            return self._unknown_after_write("set_light", exc, ["lighting_write"])
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
        if self._verified_identity != self.transport.connection_identity:
            await self._verify_qd35_target()
        await self.transport.send(c.MEDIA_CONTROL, bytes((c.MEDIA_ACTIONS[action],)))
        self._transaction_mutated = True
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
    async def sync_clock(
        self,
        clock_format: str = "preserve",
        timezone: str | None = None,
    ) -> dict[str, object]:
        self._write_allowed()
        clock_format = clock_format.strip().lower()
        if clock_format not in {"preserve", "12h", "24h"}:
            raise ValueError("clock_format must be preserve, 12h, or 24h")
        timezone_name = validate_timezone(timezone or self._timezone)
        zone = ZoneInfo(timezone_name)
        capabilities = parse_capabilities(await self._request(c.QUERY_CAPABILITIES))
        support = capabilities["supported"]
        if not support["clock_hms_format"] and not support["clock_unix_seconds"]:
            raise ProtocolError("D8 does not advertise a supported clock synchronization dialect")
        if support["clock_unix_seconds"] and not support["clock_hms_format"]:
            if clock_format != "preserve":
                raise ValueError(
                    "this firmware's Unix-seconds clock dialect cannot select 12h/24h format"
                )
            now = datetime.now().astimezone(zone)
            payload = int(now.timestamp()).to_bytes(4, "big")
            response = await self._request(c.SYNC_CLOCK, payload)
            if response != b"\x01":
                raise ProtocolError(
                    f"clock sync returned unexpected response {response.hex() or '<empty>'}"
                )
            return {
                "sent": True,
                "acknowledged": True,
                "applied": None,
                "time": now.isoformat(),
                "format": "preserve",
                "timezone": timezone_name,
                "dialect": "unix_seconds",
                "response": response.hex(),
                "attempts": 1,
                "verification": "speaker returned success; this protocol has no clock readback",
            }
        for attempt in range(1, 3):
            now = datetime.now().astimezone(zone)
            if clock_format == "preserve":
                payload = bytes((now.hour, now.minute, now.second, 0))
            elif clock_format == "12h":
                payload = bytes(
                    (now.hour, now.minute, now.second, 1, 1 if now.hour >= 12 else 0)
                )
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
                    "timezone": timezone_name,
                    "dialect": "hour_minute_second_format",
                    "response": response.hex(),
                    "attempts": attempt,
                    "verification": "speaker returned success; this protocol has no clock readback",
                }
            if response != b"\x02":
                raise ProtocolError(
                    f"clock sync returned unexpected response {response.hex() or '<empty>'}"
                )
        raise ProtocolError("speaker requested clock resynchronization twice")

    async def set_timezone(
        self,
        timezone: str,
        clock_format: str = "preserve",
        sync_now: bool = True,
    ) -> dict[str, object]:
        normalized = await asyncio.to_thread(
            persist_timezone, timezone, self._timezone_state_path
        )
        previous = self._timezone
        self._timezone = normalized
        result: dict[str, object] = {
            "timezone": normalized,
            "previous_timezone": previous,
            "persisted": True,
            "state_path": str(self._timezone_state_path),
        }
        if sync_now:
            result["clock"] = await self.sync_clock(clock_format, normalized)
        return result

    @serialized_mutation
    async def set_ldac(self, mode: str) -> dict[str, object]:
        self._write_allowed()
        mode = mode.strip().lower()
        if mode not in c.LDAC_MODES:
            raise ValueError(f"mode must be one of: {', '.join(c.LDAC_MODES)}")
        capabilities = parse_capabilities(await self._request(c.QUERY_CAPABILITIES))
        if not capabilities["supported"]["ldac"]:
            raise ProtocolError("D8 does not advertise LDAC preference support")
        expected = c.LDAC_MODES[mode]
        before = parse_ldac(await self._request(c.QUERY_LDAC))
        if before["value"] == expected:
            return {
                "applied": True,
                "changed": False,
                "bluetooth_restarted": False,
                "ldac": before,
            }
        mutation_sent = False
        try:
            await self._verify_qd35_target()
            mutation_sent = True
            await self._request(c.SET_LDAC, bytes((expected,)))
            # Some firmware acknowledges before its Bluetooth stack restarts.
            await asyncio.sleep(0.25)
            result = parse_ldac(await self._request(c.QUERY_LDAC))
            return {"applied": result["value"] == expected, "changed": True, "ldac": result}
        except QD35Error as exc:
            observed_disconnect = (
                exc.connection_dropped
                if exc.connection_dropped is not None
                else None
            )
            await self.transport.disconnect()
            return {
                "applied": None,
                "changed": None,
                "mutation_sent": mutation_sent,
                "bluetooth_restarted": True if observed_disconnect is True else None,
                "connection_dropped": observed_disconnect,
                "requested_mode": mode,
                "outcome": "unknown_until_reconnected",
                "error": str(exc),
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
        capabilities = parse_capabilities(await self._request(c.QUERY_CAPABILITIES))
        rename_capability = capabilities["rename"]
        maximum = rename_capability["max_utf8_bytes"]
        if maximum is None:
            raise ProtocolError("D8 does not advertise device rename support")
        if rename_capability["packaged_write"]:
            raise ProtocolError(
                "this rename mode requires app-style packaged writes that are not "
                "verified for QD35; refusing an unpackaged rename"
            )
        if not encoded or len(encoded) > maximum:
            raise ValueError(f"name must be 1 to {maximum} UTF-8 bytes")
        await self._request(c.SET_NAME, encoded)
        try:
            result = (await self._request(c.QUERY_NAME)).decode("utf-8", "replace")
        except QD35Error as exc:
            return {
                "applied": None,
                "requested_name": normalized,
                "outcome": "unknown_until_reconnected",
                "bluetooth_restarted": (
                    True
                    if rename_capability["reboots_bluetooth"] and not self.transport.connected
                    else None
                ),
                "error": str(exc),
            }
        return {
            "applied": result == normalized,
            "name": result,
            "max_utf8_bytes": maximum,
            "packaged_write_required_by_app": rename_capability["packaged_write"],
        }

    @serialized_mutation
    async def reset_light(self, effect: str | None = None) -> dict[str, object]:
        self._write_allowed()
        before = parse_light(await self._request(c.QUERY_LIGHT))
        effect_id = before["selected_effect_id"]
        if effect is not None:
            normalized = effect.strip().lower()
            if normalized not in c.LIGHT_EFFECTS:
                raise ValueError(f"effect must be one of: {', '.join(c.LIGHT_EFFECTS)}")
            effect_id = c.LIGHT_EFFECTS[normalized]
        response = await self._request(c.RESET_LIGHT, bytes((before["group"], effect_id)))
        try:
            after = parse_light(await self._request(c.QUERY_LIGHT))
        except QD35Error as exc:
            return self._unknown_after_write("reset_light", exc, ["reset_write"])
        return {
            "sent": True,
            "acknowledgement_hex": response.hex(),
            "effect_id": effect_id,
            "lighting": after,
            "changed": before != after,
            "verification": "state was queried after the APK-defined reset command",
        }

    @serialized_mutation
    async def set_light_timer(self, index: int, value: int) -> dict[str, object]:
        self._write_allowed()
        if not 0 <= index <= 255 or not 0 <= value <= 255:
            raise ValueError("index and value must each be between 0 and 255")
        capabilities = parse_capabilities(await self._request(c.QUERY_CAPABILITIES))
        if not capabilities["supported"]["light_timer"]:
            raise ProtocolError("D8 does not advertise ambient-light timer support")
        await self._request(c.SET_LIGHT_TIMER, bytes((index, value)))
        try:
            result = parse_light_timer(await self._request(c.QUERY_LIGHT_TIMER))
        except QD35Error as exc:
            return self._unknown_after_write("set_light_timer", exc, ["timer_write"])
        return {"applied": result == {"index": index, "value": value}, "light_timer": result}

    @staticmethod
    def _unknown_after_write(
        operation: str, exc: QD35Error, completed_steps: list[str]
    ) -> dict[str, object]:
        return {
            "applied": None,
            "outcome": "unknown_after_write",
            "operation": operation,
            "completed_steps": completed_steps,
            "verification_failed": True,
            "error": str(exc),
            "retry_automatically": False,
        }

    @serialized_mutation
    async def apply_custom_light(
        self,
        preset: str,
        palette_key: int | None = None,
        activate: bool = True,
        activation_brightness: int | None = None,
        _animation: GeneratedAnimation | None = None,
    ) -> dict[str, object]:
        """Upload a verified official or bounded diagnostic animation using 79/7A/7B."""
        self._write_allowed()
        preset = (
            _animation.name
            if _animation is not None
            else preset.strip().lower().replace("-", "_")
        )
        if activation_brightness is not None and not 1 <= activation_brightness <= 100:
            raise ValueError("activation_brightness must be between 1 and 100")
        capabilities = parse_capabilities(await self._request(c.QUERY_CAPABILITIES))
        if not capabilities["supported"]["lighting"]:
            raise ProtocolError("D8 does not advertise ambient lighting support")
        await self._verify_qd35_target()
        firmware = parse_firmware(await self._request(c.QUERY_FIRMWARE))
        if firmware != "1.3.0":
            raise ProtocolError(
                f"official custom-light assets are verified only for QD35 firmware 1.3.0, "
                f"not {firmware}"
            )
        lighting_before = parse_light(await self._request(c.QUERY_LIGHT))
        if lighting_before["group"] != 1 or not any(
            record["id"] == 7 for record in lighting_before["effects"]
        ):
            raise ProtocolError("speaker does not report QD35 group 1 Extended lighting")
        custom_before = parse_custom_light_status(await self._request(c.QUERY_CUSTOM_LIGHT))
        generated: dict[str, object] | None = None
        if _animation is not None:
            if palette_key is not None:
                raise ValueError("authored animations do not accept palette_key")
            animation = _animation
            data = animation.data
            generated = animation.to_dict()
            asset = CustomLightAsset(
                name=animation.name,
                asset_id=animation.asset_id,
                light_type=animation.light_type,
                url="generated://authored",
                sha256=animation.sha256,
                start_asset_id=animation.start_asset_id,
            )
        elif preset in DIAGNOSTIC_ANIMATION_NAMES:
            if palette_key is not None:
                raise ValueError("diagnostic animations do not accept palette_key")
            animation = build_diagnostic_animation(preset)
            data = animation.data
            generated = animation.to_dict()
            asset = CustomLightAsset(
                name=preset,
                asset_id=animation.asset_id,
                light_type=animation.light_type,
                url="generated://diagnostic",
                sha256=animation.sha256,
                start_asset_id=animation.start_asset_id,
            )
        else:
            asset, data = await asyncio.to_thread(fetch_custom_light_asset, preset, palette_key)
        write_capacity = await self.transport.prepare_large_write(163)
        if self.transport.connection_identity != self._transaction_identity:
            raise ProtocolError("Bluetooth session changed during custom-light MTU preparation")
        start_id = asset.start_asset_id or asset.asset_id
        start_payload = bytes((start_id,))
        if palette_key is not None:
            start_payload += bytes((palette_key,))
        start: dict[str, object] | None = None
        chunk_count = 0
        completed_chunks = 0
        acknowledgement_details: list[int] = []
        start_submitted = False
        try:
            start_submitted = True
            start = parse_custom_light_start(
                await self._request(c.START_CUSTOM_LIGHT_TRANSFER, start_payload)
            )
            transfer_limit = int(start["transfer_limit"])
            chunk_size = transfer_limit - 5
            if not 1 <= chunk_size <= 507:
                raise ProtocolError(
                    f"speaker returned invalid custom-light transfer limit {transfer_limit}"
                )
            required_frame_size = chunk_size + 13
            if required_frame_size > write_capacity:
                raise ProtocolError(
                    f"custom-light frame needs {required_frame_size} bytes but negotiated "
                    f"ATT capacity is {write_capacity}"
                )
            if len(data) > int(start["device_size"]):
                raise ProtocolError(
                    f"animation is {len(data)} bytes but speaker reports "
                    f"{start['device_size']} bytes"
                )
            chunk_count = (len(data) + chunk_size - 1) // chunk_size
            if chunk_count > 255:
                raise ProtocolError(
                    f"transfer needs {chunk_count} chunks but the sequence field is one byte"
                )
            for sequence in range(1, chunk_count + 1):
                offset = (sequence - 1) * chunk_size
                chunk = data[offset : offset + chunk_size].ljust(chunk_size, b"\x00")
                payload = bytes((asset.light_type, 7, sequence, 0, 37)) + chunk + b"\x00\x00"
                progress: dict[str, object] | None = None
                acknowledged = False
                for attempt in range(10):
                    try:
                        progress = parse_custom_light_progress(
                            await self._request(c.PROCESS_CUSTOM_LIGHT_TRANSFER, payload)
                        )
                    except RequestTimeout:
                        if attempt == 9:
                            raise ProtocolError(
                                f"custom-light chunk {sequence} timed out ten times"
                            ) from None
                        continue
                    detail = int(progress["detail"])
                    if progress["success"] and detail != sequence - 1:
                        # A delayed acknowledgement for the preceding chunk must not
                        # advance this sequence. Live QD35 replies use a zero-based
                        # chunk index here (00 for sequence 1, 01 for sequence 2).
                        continue
                    if progress["success"]:
                        acknowledgement_details.append(detail)
                        acknowledged = True
                        break
                if progress is None or not acknowledged:
                    raise ProtocolError(
                        f"speaker rejected custom-light chunk {sequence} ten times"
                    )
                completed_chunks = sequence
            finish = parse_custom_light_finish(
                await self._request(c.FINISH_CUSTOM_LIGHT_TRANSFER, b"\x01")
            )
            if not finish["success"]:
                raise ProtocolError(
                    f"custom-light completion failed with status {finish['status']}"
                )
        except asyncio.CancelledError:
            if start_submitted:
                await asyncio.shield(self._best_effort_custom_light_abort())
            raise
        except QD35Error as exc:
            aborted = await self._best_effort_custom_light_abort() if start_submitted else False
            return {
                "applied": None,
                "outcome": "unknown",
                "preset": preset,
                "completed_chunks": completed_chunks,
                "total_chunks": chunk_count,
                "abort_acknowledged": aborted,
                "connection_dropped": (
                    exc.connection_dropped
                    if isinstance(exc, OutcomeUnknown)
                    else not self.transport.connected
                ),
                "error": str(exc),
                "retry_automatically": False,
            }
        await asyncio.sleep(0.5)
        try:
            custom_after = parse_custom_light_status(
                await self._request(c.QUERY_CUSTOM_LIGHT)
            )
        except QD35Error as exc:
            return {
                "applied": None,
                "outcome": "transfer_completed_but_readback_failed",
                "preset": preset,
                "palette_key": palette_key,
                "bytes_uploaded": len(data),
                "chunks_uploaded": completed_chunks,
                "completion_acknowledged": True,
                "activation_requested": activate,
                "activated": None,
                "custom_light_before": custom_before,
                "error": str(exc),
                "retry_automatically": False,
            }
        installed = custom_after["asset_id"] == start_id and (
            palette_key is None or custom_after["palette_key"] == palette_key
        )
        try:
            lighting_after = parse_light(await self._request(c.QUERY_LIGHT))
            extended = next(
                (record for record in lighting_after["effects"] if record["id"] == 7),
                None,
            )
            if extended is None:
                raise ProtocolError("lighting readback omitted Extended effect 7")
            if (
                activate
                and installed
                and (
                    lighting_after["selected_effect_id"] != 7
                    or extended["raw_color"] != [1, 0, 0]
                    or extended["brightness"] == 0
                    or (
                        activation_brightness is not None
                        and extended["brightness"] != activation_brightness
                    )
                )
            ):
                payload = bytes(
                    (
                        lighting_after["group"],
                        7,
                        1,
                        0,
                        0,
                        activation_brightness or extended["brightness"] or 100,
                        extended["wire_speed"],
                    )
                )
                await self._request(c.SET_LIGHT, payload)
                lighting_after = parse_light(await self._request(c.QUERY_LIGHT))
        except QD35Error as exc:
            return {
                "applied": installed,
                "preset": preset,
                "palette_key": palette_key,
                "asset_sha256": asset.sha256,
                "bytes_uploaded": len(data),
                "chunks_uploaded": completed_chunks,
                "completion_acknowledged": True,
                "custom_light_before": custom_before,
                "custom_light_after": custom_after,
                "activation_requested": activate,
                "activated": None,
                "activation_outcome": "unknown_after_install",
                "error": str(exc),
                "retry_automatically": False,
            }
        extended_after = next(
            (record for record in lighting_after["effects"] if record["id"] == 7), None
        )
        activated = bool(
            installed
            and extended_after is not None
            and lighting_after["selected_effect_id"] == 7
            and extended_after["raw_color"] == [1, 0, 0]
            and extended_after["brightness"] > 0
            and (
                activation_brightness is None
                or extended_after["brightness"] == activation_brightness
            )
        )
        return {
            "applied": installed,
            "preset": preset,
            "palette_key": palette_key,
            "asset_sha256": asset.sha256,
            "generated_animation": generated,
            "bytes_uploaded": len(data),
            "chunks_uploaded": completed_chunks,
            "transfer": start,
            "chunk_ack_details": acknowledgement_details,
            "all_chunk_acks_sequence_correlated": all(
                detail == sequence - 1
                for sequence, detail in enumerate(acknowledgement_details, start=1)
            ),
            "custom_light_before": custom_before,
            "custom_light_after": custom_after,
            "activated": activated,
            "lighting": lighting_after,
            "verification": (
                "0x78 verifies asset identity/palette; firmware does not return the "
                "installed binary"
            ),
        }

    async def apply_authored_light(
        self,
        name: str,
        frames: Sequence[tuple[int, Sequence[str]]],
        activate: bool = True,
        activation_brightness: int | None = None,
    ) -> dict[str, object]:
        """Compile and upload a local, structurally bounded RGB animation."""
        animation = compile_authored_animation(name, frames)
        return await self.apply_custom_light(
            animation.name,
            None,
            activate,
            activation_brightness,
            animation,
        )

    async def apply_animation_preset(
        self,
        preset: str,
        activate: bool = True,
        activation_brightness: int | None = None,
    ) -> dict[str, object]:
        """Generate and upload a bounded creative animation preset."""
        animation = build_creative_animation(preset)
        return await self.apply_custom_light(
            animation.name,
            None,
            activate,
            activation_brightness,
            animation,
        )

    async def apply_reactive_light(
        self,
        name: str,
        bands: Sequence[Sequence[Sequence[str]]],
        activate: bool = True,
        activation_brightness: int | None = None,
    ) -> dict[str, object]:
        """Compile and upload the firmware's bounded audio-reactive RGB layout."""
        animation = compile_reactive_animation(name, bands)
        return await self.apply_custom_light(
            animation.name, None, activate, activation_brightness, animation
        )

    async def _best_effort_custom_light_abort(self) -> bool:
        if not self.transport.connected:
            return False
        try:
            result = parse_custom_light_finish(
                await self.transport.request(
                    c.FINISH_CUSTOM_LIGHT_TRANSFER,
                    b"\x02",
                    self.config.request_timeout,
                )
            )
            return bool(result["success"])
        except QD35Error:
            return False

    async def _disruptive(self, opcode: int, operation: str) -> dict[str, object]:
        self._write_allowed(dangerous=True)
        capability_by_opcode = {
            c.POWER_OFF: "power_off",
            c.DISCONNECT_AUDIO: "disconnect_audio",
            c.FACTORY_RESET: "factory_reset",
        }
        capability = capability_by_opcode[opcode]
        capabilities = parse_capabilities(await self._request(c.QUERY_CAPABILITIES))
        if not capabilities["supported"][capability]:
            raise ProtocolError(f"D8 does not advertise {operation} support")
        try:
            response = await self._request(opcode)
            if response != b"\x01":
                raise ProtocolError(
                    f"{operation} returned unexpected response {response.hex() or '<empty>'}"
                )
            return {"sent": True, "operation": operation, "response": response.hex()}
        except OutcomeUnknown as exc:
            observed_disconnect = (
                exc.connection_dropped
                if exc.connection_dropped is not None
                else not self.transport.connected
            )
            return {
                "sent": True,
                "operation": operation,
                "connection_dropped": observed_disconnect,
                "outcome": "unknown; verify physically or rediscover",
                "error": str(exc),
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
