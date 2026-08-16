"""Unit tests for the v2 frame layer: pure bytes, no server involved.

Frames travel over a socketpair, two connected in-memory sockets, so
these tests exercise the real send/recv path without any networking.
"""
import socket
import threading

import pytest

from retriever import protocol as H


@pytest.fixture
def pair():
    a, b = socket.socketpair()
    yield a, b
    a.close()
    b.close()


def test_frame_round_trip(pair):
    a, b = pair
    H.write_frame(a, H.T_GET, b"\x00\x07cat.png")
    frame_type, payload = H.read_frame(b)
    assert frame_type == H.T_GET
    assert payload == b"\x00\x07cat.png"


def test_empty_payload_round_trip(pair):
    a, b = pair
    H.write_frame(a, H.T_HELLO)
    assert H.read_frame(b) == (H.T_HELLO, b"")


def test_header_bytes_match_spec_worked_example(pair):
    """The HELLO frame must be byte-identical to docs/PROTOCOL.md."""
    a, b = pair
    H.write_frame(a, H.T_HELLO)
    raw = H.read_exact_bytes(b, H.HEADER_SIZE)
    assert raw == bytes.fromhex("52545256" "03" "10" "0000000000000000")


def test_wrong_magic_rejected(pair):
    a, b = pair
    a.sendall(b"HTTP" + bytes([2, H.T_LIST]) + (0).to_bytes(8, "big"))
    with pytest.raises(H.FrameError) as err:
        H.read_frame(b)
    assert err.value.reason == H.E_UNSUPPORTED_VERSION


def test_v1_first_byte_rejected_as_bad_magic(pair):
    """A v1 client's opening bytes must fail the magic check."""
    a, b = pair
    a.sendall(b"\x00" * H.HEADER_SIZE)  # v1 LIST plus padding
    with pytest.raises(H.FrameError) as err:
        H.read_frame(b)
    assert err.value.reason == H.E_UNSUPPORTED_VERSION


@pytest.mark.parametrize("version", [1, 2, 4, 255], ids=["v1", "v2", "future", "max"])
def test_wrong_version_rejected(pair, version):
    a, b = pair
    a.sendall(H.MAGIC + bytes([version, H.T_LIST]) + (0).to_bytes(8, "big"))
    with pytest.raises(H.FrameError) as err:
        H.read_frame(b)
    assert err.value.reason == H.E_UNSUPPORTED_VERSION


def test_unknown_frame_type_rejected(pair):
    a, b = pair
    a.sendall(H.MAGIC + bytes([H.VERSION, 0x42]) + (0).to_bytes(8, "big"))
    with pytest.raises(H.FrameError) as err:
        H.read_frame(b)
    assert err.value.reason == H.E_MALFORMED


def test_payload_over_cap_rejected(pair):
    a, b = pair
    too_big = H.PAYLOAD_CAPS[H.T_GET] + 1
    a.sendall(H.MAGIC + bytes([H.VERSION, H.T_GET]) + too_big.to_bytes(8, "big"))
    with pytest.raises(H.FrameError) as err:
        H.read_frame(b)
    assert err.value.reason == H.E_MALFORMED


def test_payload_at_cap_accepted(pair):
    a, b = pair
    payload = b"x" * H.PAYLOAD_CAPS[H.T_GET]
    #64 KiB overflows the kernel socket buffer, so a same-thread
    #write-then-read would deadlock; write from a helper thread instead
    writer = threading.Thread(target=H.write_frame, args=(a, H.T_GET, payload))
    writer.start()
    assert H.read_frame(b) == (H.T_GET, payload)
    writer.join(timeout=5)


def test_truncated_header_raises_connection_error(pair):
    a, b = pair
    a.sendall(H.MAGIC[:2])  # 2 of 14 header bytes, then hang up
    a.close()
    with pytest.raises(ConnectionError):
        H.read_frame(b)


def test_error_payload_round_trip():
    payload = H.pack_error(H.E_NOT_FOUND, "not found")
    assert payload == bytes.fromhex("02" "0009") + b"not found"
    assert H.unpack_error(payload) == (H.E_NOT_FOUND, "not found")


def test_error_payload_empty_message():
    assert H.unpack_error(H.pack_error(H.E_INTERNAL)) == (H.E_INTERNAL, "")


def test_error_payload_length_mismatch_rejected():
    bad = bytes([H.E_NOT_FOUND]) + (99).to_bytes(2, "big") + b"short"
    with pytest.raises(H.FrameError):
        H.unpack_error(bad)
