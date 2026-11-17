import random

import pytest

from qd35_mcp.errors import ProtocolError
from qd35_mcp.protocol import (
    FrameStreamDecoder,
    decode_frame,
    encode_frame,
    parse_address,
    parse_eq,
    parse_eq_preset,
    parse_firmware,
    parse_light,
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


def test_stream_decoder_recovers_from_plausible_but_false_length() -> None:
    incomplete = bytes.fromhex("bbec660064") + b"garbage"
    valid = reply(0xD5, b"\x00")
    items = FrameStreamDecoder().feed(incomplete + valid)
    assert isinstance(items[0], ProtocolError)
    assert items[-1].opcode == 0xD5


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
