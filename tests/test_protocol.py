import pytest

from qd35_mcp.errors import ProtocolError
from qd35_mcp.protocol import FrameStreamDecoder, decode_frame, encode_frame, parse_eq, parse_light


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
    assert [frame.opcode for frame in frames] == [0x61, 0xD5]


def test_parses_live_eq_capture() -> None:
    payload = bytes.fromhex(
        "03060000003e0607010000fa0607020003e8060703000fa0060704001f40060705003e800607"
    )
    parsed = parse_eq(payload)
    assert parsed["band_count"] == 6
    assert [band["frequency_hz"] for band in parsed["bands"]] == [62, 250, 1000, 4000, 8000, 16000]
    assert all(band["gain_db"] == 0 for band in parsed["bands"])


def test_parses_live_light_capture() -> None:
    payload = bytes.fromhex(
        "010401010000640502010000640303e6e6ff64ff04e6e6ff640305b476ff64ff06e6e6ff64ff0700000064ff"
    )
    parsed = parse_light(payload)
    assert parsed["selected_effect"] == "breathing"
    assert len(parsed["effects"]) == 7
    assert parsed["effects"][3]["speed"] == 8
