import socket
import threading
import time

import pytest

from conftest import connect_v2 as _connect_v2, name_payload as _name_payload
from retriever import client
from retriever import protocol as H


def _list(port):
    """Run a full v2 LIST exchange. Returns (frame_type, names)."""
    with _connect_v2(port) as sock:
        H.write_frame(sock, H.T_LIST)
        frame_type, payload = H.read_frame(sock)
    names = {n.decode("utf-8") for n in payload.split(b"\x00") if n}
    return frame_type, names


def test_list_returns_directory_entries(server_port, tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)

    frame_type, names = _list(server_port)

    assert frame_type == H.T_OK
    assert names == {"a.txt", "b.png"}


def test_list_of_empty_directory(server_port):
    frame_type, names = _list(server_port)

    assert frame_type == H.T_OK
    assert names == set()


def test_get_not_found_reports_reason(server_port):
    """v2's whole point: the client can tell WHY a request failed."""
    with _connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_GET, _name_payload(b"ghost.txt"))
        frame_type, payload = H.read_frame(sock)

    assert frame_type == H.T_ERROR
    reason, message = H.unpack_error(payload)
    assert reason == H.E_NOT_FOUND
    assert message  # human text present


def test_get_streams_file_body(server_port, tmp_path):
    body = bytes(range(256)) * 64  # 16 KiB, all byte values
    (tmp_path / "data.bin").write_bytes(body)

    with _connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_GET, _name_payload(b"data.bin"))
        frame_type, size = H.read_frame_header(sock)
        assert frame_type == H.T_OK
        received = H.read_exact_bytes(sock, size)

    assert received == body


def test_put_to_existing_name_rejected_before_body(server_port, tmp_path):
    """Two-step PUT: rejection happens before any body bytes travel,
    and the existing file is untouched."""
    original = b"\x89PNG\r\n\x1a\n" + b"original contents"
    existing = tmp_path / "photo.png"
    existing.write_bytes(original)

    with _connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_PUT, _name_payload(b"photo.png", 1000))
        frame_type, payload = H.read_frame(sock)

    assert frame_type == H.T_ERROR
    assert H.unpack_error(payload)[0] == H.E_ALREADY_EXISTS
    assert existing.read_bytes() == original


def test_put_dying_mid_body_leaves_nothing(server_port, tmp_path):
    """A PUT approved but killed mid-body must clean up its debris."""
    with _connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_PUT, _name_payload(b"new.bin", 1000))
        frame_type, _ = H.read_frame(sock)
        assert frame_type == H.T_OK      # permission to send
        sock.sendall(b"x" * 100)         # 100 of 1000 promised bytes
    # connection closed mid-body

    time.sleep(0.3)
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"dead PUT left files behind: {leftovers}"


def test_put_stores_complete_file(server_port, tmp_path):
    body = b"\x89PNG\r\n\x1a\n" + b"y" * 500

    with _connect_v2(server_port) as sock:
        H.write_frame(sock, H.T_PUT, _name_payload(b"up.png", len(body)))
        frame_type, _ = H.read_frame(sock)
        assert frame_type == H.T_OK
        sock.sendall(body)
        frame_type, _ = H.read_frame(sock)
        assert frame_type == H.T_OK      # stored

    assert (tmp_path / "up.png").read_bytes() == body


def test_v1_client_is_rejected_with_versioned_error(server_port):
    """Phase 1 definition of done: a v1 peer gets a clean v2 error frame.

    A v1 GET for a long-enough name supplies the 14 bytes a v2 header
    needs, and its first byte 0x01 fails the magic check immediately.
    """
    name = b"cat_photo.png"  # 13 chars: v1 GET = 1 + 2 + 13 = 16 bytes
    with socket.create_connection(("127.0.0.1", server_port), timeout=5) as sock:
        sock.sendall(b"\x01" + len(name).to_bytes(2, "big") + name)
        frame_type, payload = H.read_frame(sock)

    assert frame_type == H.T_ERROR
    assert H.unpack_error(payload)[0] == H.E_UNSUPPORTED_VERSION


def test_command_before_hello_is_malformed(server_port):
    with socket.create_connection(("127.0.0.1", server_port), timeout=5) as sock:
        H.write_frame(sock, H.T_LIST)   # skipped the handshake
        frame_type, payload = H.read_frame(sock)

    assert frame_type == H.T_ERROR
    assert H.unpack_error(payload)[0] == H.E_MALFORMED


def test_server_times_out_a_stalled_client(server_port, monkeypatch):
    """A client that connects and sends nothing must not hang the server."""
    monkeypatch.setattr(H, "SOCKET_TIMEOUT", 0.5)

    with socket.create_connection(("127.0.0.1", server_port), timeout=5) as sock:
        sock.settimeout(3)
        # send nothing; the server should give up on us and close
        data = sock.recv(64)   # raises socket.timeout if the server hangs
        assert data == b""

    # and the server must still serve the next client afterwards
    frame_type, names = _list(server_port)
    assert frame_type == H.T_OK
    assert names == set()


def test_get_leaves_no_partial_file_when_connection_dies(tmp_path, monkeypatch):
    """A GET that dies mid-download must not leave a partial file behind."""
    monkeypatch.chdir(tmp_path)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def evil_server():
        conn, _ = listener.accept()
        H.read_frame(conn)                       # HELLO
        H.write_frame(conn, H.T_OK)
        H.read_frame(conn)                       # GET request
        H.write_frame_header(conn, H.T_OK, 1000) # promise 1000 bytes...
        conn.sendall(b"x" * 100)                 # ...deliver only 100
        conn.close()                             # and hang up

    t = threading.Thread(target=evil_server, daemon=True)
    t.start()

    sock = client.connect("127.0.0.1", port)
    with pytest.raises(SystemExit) as exc:
        client.do_get(sock, "victim.bin")
    assert exc.value.code == client.EXIT_CONN
    sock.close()
    t.join(timeout=5)
    listener.close()

    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"dead GET left files behind: {leftovers}"
