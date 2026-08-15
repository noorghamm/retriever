"""Attack tests: malformed, boundary, and hostile inputs against the
real server. Every case asserts the server answers with the exact error
reason the spec promises, and never hangs or crashes.
"""
import socket

import pytest

from conftest import connect_v2, name_payload
from retriever import protocol as H


def _expect_error(sock, reason):
    frame_type, payload = H.read_frame(sock)
    assert frame_type == H.T_ERROR
    got_reason, message = H.unpack_error(payload)
    assert got_reason == reason, f"expected reason {reason}, got {got_reason}: {message}"
    return message


#--- raw-bytes attacks: no valid framing at all ----------------------------

@pytest.mark.parametrize("raw", [
    pytest.param(b"GARBAGEGARBAGE", id="ascii-garbage"),
    pytest.param(bytes(range(14)), id="binary-garbage"),
    pytest.param(b"GET / HTTP/1.1\r\n", id="http-request"),
    pytest.param(b"\x00" * 14, id="v1-list-with-padding"),
], )
def test_non_v2_bytes_get_versioned_error(server_port, raw):
    with socket.create_connection(("127.0.0.1", server_port), timeout=5) as sock:
        sock.sendall(raw)
        _expect_error(sock, H.E_UNSUPPORTED_VERSION)


def test_future_version_rejected(server_port):
    with socket.create_connection(("127.0.0.1", server_port), timeout=5) as sock:
        sock.sendall(H.MAGIC + bytes([9, H.T_HELLO]) + (0).to_bytes(8, "big"))
        _expect_error(sock, H.E_UNSUPPORTED_VERSION)


def test_unknown_frame_type_rejected(server_port):
    with socket.create_connection(("127.0.0.1", server_port), timeout=5) as sock:
        sock.sendall(H.MAGIC + bytes([H.VERSION, 0x7F]) + (0).to_bytes(8, "big"))
        _expect_error(sock, H.E_MALFORMED)


def test_reply_type_from_client_rejected(server_port):
    """A client has no business sending OK frames."""
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_OK)
        _expect_error(sock, H.E_MALFORMED)


def test_oversized_payload_claim_rejected(server_port):
    """A header claiming a payload over the cap is refused at the header."""
    with connect_v2(server_port) as sock:
        too_big = H.PAYLOAD_CAPS[H.T_GET] + 1
        sock.sendall(H.MAGIC + bytes([H.VERSION, H.T_GET]) + too_big.to_bytes(8, "big"))
        _expect_error(sock, H.E_MALFORMED)


#--- malformed command payloads --------------------------------------------

@pytest.mark.parametrize("payload", [
    pytest.param(b"", id="empty-payload"),
    pytest.param(b"\x00", id="one-byte-payload"),
    pytest.param((30).to_bytes(2, "big") + b"short", id="name-len-lies"),
    pytest.param(name_payload(b"ok.txt") + b"extra", id="trailing-bytes"),
], )
def test_malformed_get_payloads(server_port, payload):
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_GET, payload)
        _expect_error(sock, H.E_MALFORMED)


def test_put_without_size_field_rejected(server_port):
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_PUT, name_payload(b"file.bin"))  # no u64 size
        _expect_error(sock, H.E_MALFORMED)


#--- filename boundaries ----------------------------------------------------

@pytest.mark.parametrize("name", [
    pytest.param(b"", id="empty-name"),
    pytest.param(b"x" * 256, id="name-too-long"),
    pytest.param(b"../etc/passwd", id="path-traversal"),
    pytest.param(b"a\\b.txt", id="backslash"),
    pytest.param(b"sneaky..txt", id="dotdot-substring"),
    pytest.param("café.txt".encode("utf-8"), id="non-ascii"),
    pytest.param(b"tab\there.txt", id="control-char"),
], )
def test_bad_names_rejected_on_get(server_port, name):
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_GET, name_payload(name))
        _expect_error(sock, H.E_BAD_NAME)


def test_longest_legal_name_is_accepted(server_port):
    """255 chars passes validation and gets NOT_FOUND, not BAD_NAME."""
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_GET, name_payload(b"x" * 255))
        _expect_error(sock, H.E_NOT_FOUND)


#--- size boundaries --------------------------------------------------------

def test_put_empty_file(server_port, tmp_path):
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_PUT, name_payload(b"empty.bin", 0))
        frame_type, _ = H.read_frame(sock)
        assert frame_type == H.T_OK          # permission to send
        frame_type, _ = H.read_frame(sock)   # nothing to stream; verdict is next
        assert frame_type == H.T_OK
    assert (tmp_path / "empty.bin").read_bytes() == b""


def test_get_empty_file(server_port, tmp_path):
    (tmp_path / "empty.bin").write_bytes(b"")
    with connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_GET, name_payload(b"empty.bin"))
        frame_type, size = H.read_frame_header(sock)
        assert frame_type == H.T_OK
        assert size == 0
