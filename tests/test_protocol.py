import random

import pytest

from qd35_mcp.errors import ProtocolError
from qd35_mcp.protocol import (
    FrameStreamDecoder,
    decode_frame,
    encode_frame,
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
    parse_light,
    parse_light_setting,
    parse_light_timer,
    parse_media_info,
    parse_playback,
    parse_volume,
)


def reply(opcode: int, payload: bytes) -> bytes:
    data = bytearray((0xBB, 0xEC, opcode, len(payload) >> 8, len(payload) & 0xFF))
    data.extend(payload)
    data.append(sum(data) & 0xFF)
    return bytes(data)


def test_live_volume_query_frame() -> None:
    assert encode_frame(0x66).hex() == "aaec660000fc"
    frame = decode_frame(bytes.fromhex("bbec660002100a29"))
    assert frame.opcode == 0x66
    assert frame.payload == bytes((16, 10))


def test_xor_applies_only_to_payload() -> None:
    assert encode_frame(0x67, b"\x0a", xor_payload=True).hex() == "aaec670001afaD".lower()


def test_bad_checksum_is_rejected() -> None:
    with pytest.raises(ProtocolError, match="checksum"):
        decode_frame(bytes.fromhex("bbec660002100a00"))


def test_stream_decoder_handles_fragments_and_unsolicited_frames() -> None:
    one = reply(0x61, b"\x02\x01")
    two = reply(0xD5, b"\x00")
    decoder = FrameStreamDecoder()
    assert decoder.feed(one[:4]) == []
    frames = decoder.feed(one[4:] + two)
    assert all(not isinstance(frame, ProtocolError) for frame in frames)
    assert [frame.opcode for frame in frames] == [0x61, 0xD5]


def test_stream_decoder_reports_corruption_and_recovers_valid_frame() -> None:
    corrupt = bytearray(reply(0x66, b"\x10\x0a"))
    corrupt[-1] = 0
    valid = reply(0x61, b"\x02\x01")
    items = FrameStreamDecoder().feed(bytes(corrupt) + valid)
    assert isinstance(items[0], ProtocolError)
    assert items[-1].opcode == 0x61


def test_stream_decoder_rejects_implausible_length_without_blocking() -> None:
    invalid_header = bytes.fromhex("bbec66ffff")
    valid = reply(0xD5, b"\x00")
    items = FrameStreamDecoder().feed(invalid_header + valid)
    assert isinstance(items[0], ProtocolError)
    assert items[-1].opcode == 0xD5


def test_stream_decoder_preserves_plausible_fragment_until_explicit_reset() -> None:
    incomplete = bytes.fromhex("bbec660064") + b"garbage"
    valid = reply(0xD5, b"\x00")
    decoder = FrameStreamDecoder()
    assert decoder.feed(incomplete + valid) == []
    decoder.reset()
    items = decoder.feed(valid)
    assert items[-1].opcode == 0xD5


def test_stream_decoder_does_not_promote_embedded_frame_from_fragmented_payload() -> None:
    inner = reply(0x66, b"\x10\x06")
    outer = reply(0x50, b"prefix" + inner + b"suffix")
    decoder = FrameStreamDecoder()
    split = 5 + len(b"prefix") + len(inner)
    assert decoder.feed(outer[:split]) == []
    items = decoder.feed(outer[split:])
    assert len(items) == 1
    assert items[0].opcode == 0x50
    assert items[0].payload == b"prefix" + inner + b"suffix"


def test_parses_live_eq_capture() -> None:
    payload = bytes.fromhex(
        "03060000003e0607010000fa0607020003e8060703000fa0060704001f40060705003e800607"
    )
    parsed = parse_eq(payload)
    assert parsed["band_count"] == 6
    assert [band["frequency_hz"] for band in parsed["bands"]] == [62, 250, 1000, 4000, 8000, 16000]
    assert all(band["gain_db"] == 0 for band in parsed["bands"])


def test_parses_live_eq_capture_with_firmware_padding() -> None:
    payload = bytes.fromhex(
        "03060000003e0607010000fa0607020003e8060703000fa0060704001f40060705003e80060700000000"
    )
    assert parse_eq(payload)["band_count"] == 6


def test_parses_live_light_capture() -> None:
    payload = bytes.fromhex(
        "010401010000640502010000640303e6e6ff64ff04e6e6ff640305b476ff64ff06e6e6ff64ff0700000064ff"
    )
    parsed = parse_light(payload)
    assert parsed["selected_effect"] == "breathing"
    assert len(parsed["effects"]) == 7
    assert parsed["effects"][0]["color_control"] == "mode"
    assert parsed["effects"][0]["color_mode"] == "light"
    assert parsed["effects"][0]["rgb"] is None
    assert parsed["effects"][3]["color_control"] == "rgb"
    assert parsed["effects"][3]["rgb"] == "#E6E6FF"
    assert parsed["effects"][3]["speed"] == 8


def test_truncated_structured_payloads_are_rejected() -> None:
    with pytest.raises(ProtocolError, match="declares 6 bands"):
        parse_eq(b"\x03\x06")
    with pytest.raises(ProtocolError, match="truncated effect"):
        parse_light(b"\x01\x04\x04\xff")


def test_eq_rejects_duplicate_or_missing_band_ids() -> None:
    record = bytes((0, 0, 0, 62, 6, 7))
    with pytest.raises(ProtocolError, match="band IDs"):
        parse_eq(bytes((3, 2)) + record + record)


def test_structured_parsers_reject_impossible_device_state() -> None:
    with pytest.raises(ProtocolError, match="invalid volume range"):
        parse_volume(bytes((16, 17)))
    with pytest.raises(ProtocolError, match="trailing data"):
        parse_eq(bytes((3, 1, 0, 0, 0, 62, 6, 7, 0)))
    with pytest.raises(ProtocolError, match="gain code"):
        parse_eq(bytes((3, 1, 0, 0, 0, 62, 13, 7)))
    with pytest.raises(ProtocolError, match="brightness"):
        parse_light(bytes((1, 4, 4, 1, 2, 3, 101, 3)))
    with pytest.raises(ProtocolError, match="wire speed"):
        parse_light(bytes((1, 4, 4, 1, 2, 3, 100, 0)))


def test_fixed_width_parsers_reject_trailing_bytes() -> None:
    for parser, payload in (
        (parse_volume, b"\x10\x06\x00"),
        (parse_eq_preset, b"\x03\x00"),
    ):
        with pytest.raises(ProtocolError, match="exactly"):
            parser(payload)


def test_identity_parsers_reject_empty_or_wrong_sized_values() -> None:
    with pytest.raises(ProtocolError, match="firmware response is empty"):
        parse_firmware(b"")
    with pytest.raises(ProtocolError, match="needs 6 bytes"):
        parse_address(b"\x01\x02\x03")
    assert parse_address(bytes.fromhex("aabbccddeeff")) == "AA:BB:CC:DD:EE:FF"


def test_playback_parser_uses_app_confirmed_playing_value() -> None:
    assert parse_playback(b"\x0d") == {"value": 13, "playing": True}
    assert parse_playback(b"\x03") == {"value": 3, "playing": False}
    with pytest.raises(ProtocolError, match="exactly 1"):
        parse_playback(b"")


def test_decodes_live_d8_capabilities() -> None:
    parsed = parse_capabilities(bytes.fromhex("0000010401010100000001010a0c0a00002004830401"))
    assert parsed["length"] == 22
    assert parsed["rename"] == {
        "mode": 4,
        "max_utf8_bytes": 35,
        "packaged_write": False,
        "package_size": None,
        "reboots_bluetooth": True,
    }
    assert parsed["eq_profile"] == 12
    assert parsed["skin_profile"] == 10
    assert parsed["supported"]["ldac"] is True
    assert parsed["supported"]["clock_hms_format"] is True
    assert parsed["supported"]["light_timer"] is False
    assert parsed["supported"]["no_audio_auto_shutdown"] is False
    assert parsed["supported"]["shutdown_timer"] is False
    assert parsed["supported"]["separate_light_color"] is False


def test_d8_is_length_aware_and_preserves_unknown_tail() -> None:
    parsed = parse_capabilities(bytes((0, 0, 1, 6)) + bytes(19))
    assert parsed["rename"]["max_utf8_bytes"] == 24
    assert parsed["rename"]["reboots_bluetooth"] is False
    assert parsed["unknown_trailing_hex"] == "00"
    with pytest.raises(ProtocolError, match="empty"):
        parse_capabilities(b"")


def test_parses_status_timer_media_and_custom_light_payloads() -> None:
    assert parse_audio_status(b"\x03") == {"value": 3, "logos": [], "known": False}
    assert parse_audio_status(b"\x0c") == {
        "value": 12,
        "logos": ["hwa", "hi_res"],
        "known": True,
    }
    assert parse_custom_light_status(b"\x01\x07") == {
        "asset_id": 1,
        "detail": 7,
        "palette_key": 7,
    }
    assert parse_custom_light_status(b"\x17\xaa") == {
        "asset_id": 23,
        "detail": 170,
        "palette_key": None,
    }
    assert parse_light_timer(b"\x02\x1e") == {"index": 2, "value": 30}
    assert parse_media_info(b"\x01\x04\x03SongDJX") == {
        "playing": True,
        "title": "Song",
        "artist": "DJX",
        "metadata_present": True,
    }
    assert parse_media_info(b"\x00") == {
        "playing": False,
        "title": None,
        "artist": None,
        "metadata_present": False,
    }
    assert parse_custom_light_status(b"\x17") == {
        "asset_id": 23,
        "detail": None,
        "palette_key": None,
    }
    assert parse_light_setting(bytes.fromhex("010604050632ff")) == {
        "group": 1,
        "effect_id": 6,
        "effect": "static",
        "raw_color": [4, 5, 6],
        "brightness": 50,
        "speed": None,
        "wire_speed": 255,
    }
    start = parse_custom_light_start(bytes.fromhex("01020304002500001000"))
    assert start["version"] == "1.2.3"
    assert start["transfer_limit"] == 37
    assert start["device_size"] == 4096
    assert parse_custom_light_progress(b"\x00\x05")["success"] is True
    assert parse_custom_light_finish(b"\x01")["success"] is True


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_audio_status, b""),
        (parse_custom_light_status, b""),
        (parse_light_timer, b"\x00"),
        (parse_media_info, b"\x01\x04\x00abc"),
        (parse_custom_light_start, b"\x00" * 9),
        (parse_custom_light_progress, b"\x00"),
        (parse_custom_light_finish, b""),
    ],
)
def test_new_parsers_reject_truncated_payloads(parser: object, payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        parser(payload)  # type: ignore[operator]


@pytest.mark.parametrize("xor_payload", [False, True])
def test_frame_roundtrips_random_payloads(xor_payload: bool) -> None:
    randomizer = random.Random(0xD35 + xor_payload)
    for size in (0, 1, 2, 7, 20, 255, 1024):
        opcode = randomizer.randrange(256)
        payload = randomizer.randbytes(size)
        frame = decode_frame(encode_frame(opcode, payload, xor_payload), xor_payload)
        assert frame.opcode == opcode
        assert frame.payload == payload


@pytest.mark.parametrize("seed", range(25))
def test_stream_decoder_handles_random_fragmentation(seed: int) -> None:
    randomizer = random.Random(seed)
    expected = [
        (randomizer.randrange(256), randomizer.randbytes(randomizer.randrange(33)))
        for _ in range(20)
    ]
    stream = b"".join(reply(opcode, payload) for opcode, payload in expected)
    decoder = FrameStreamDecoder()
    decoded = []
    while stream:
        width = randomizer.randrange(1, min(18, len(stream)) + 1)
        decoded.extend(decoder.feed(stream[:width]))
        stream = stream[width:]
    assert all(not isinstance(item, ProtocolError) for item in decoded)
    assert [(item.opcode, item.payload) for item in decoded] == expected


def test_structured_parsers_never_leak_builtin_exceptions() -> None:
    randomizer = random.Random(0xB0D)
    for parser in (parse_eq, parse_light):
        for _ in range(5000):
            payload = randomizer.randbytes(randomizer.randrange(65))
            try:
                parser(payload)
            except ProtocolError:
                pass


def test_stream_decoder_bounds_buffer_under_random_noise() -> None:
    randomizer = random.Random(0xD3C0DE)
    decoder = FrameStreamDecoder(max_payload=64)
    for _ in range(1000):
        decoder.feed(randomizer.randbytes(randomizer.randrange(1, 65)))
        # At most a plausible incomplete frame can remain buffered.
        assert len(decoder._buffer) <= 70
