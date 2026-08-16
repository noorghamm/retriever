"""Shared fixtures and helpers for the retriever test suite."""
import hashlib
import socket
import threading
import time

import pytest

from retriever import protocol as H
from retriever.server import start_server


def free_port():
    """Ask the OS for a currently free port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server_port(tmp_path, monkeypatch):
    """Run the real server in a background thread, serving a temp directory.

    Yields the port it listens on. The thread is a daemon: the server has
    no shutdown mechanism yet, so it dies with the test process.
    """
    monkeypatch.chdir(tmp_path)
    port = free_port()
    threading.Thread(target=start_server, args=(port,), daemon=True).start()

    # wait until the server is accepting connections
    deadline = time.time() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if time.time() > deadline:
                pytest.fail("server did not start listening within 5s")
            time.sleep(0.05)
    yield port


def connect_v2(port):
    """Open a connection and complete the HELLO handshake."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    H.write_frame(sock, H.T_HELLO)
    frame_type, _ = H.read_frame(sock)
    assert frame_type == H.T_OK, "HELLO was not accepted"
    return sock


def sha(body):
    """SHA-256 digest of some bytes."""
    return hashlib.sha256(body).digest()


def get_payload(name, offset=0, token=None):
    """v3 GET payload: u64 offset, 8-byte resume token, u16 name_len, name."""
    token = token if token is not None else b"\0" * H.TOKEN_SIZE
    return (offset.to_bytes(8, "big") + token
            + len(name).to_bytes(2, "big") + name)


def read_get(sock):
    """Read a whole v3 GET reply.

    Returns (frame_type, info, body), where info is (total_size,
    start_offset, digest) on success or (reason, message) on error.
    """
    frame_type, payload_len = H.read_frame_header(sock)
    if frame_type == H.T_ERROR:
        return frame_type, H.unpack_error(H.read_exact_bytes(sock, payload_len)), b""
    meta = H.read_exact_bytes(sock, H.GET_META)
    info = (int.from_bytes(meta[:8], "big"),
            int.from_bytes(meta[8:16], "big"),
            meta[16:])
    body = H.read_exact_bytes(sock, payload_len - H.GET_META)
    return frame_type, info, body


def put_payload(name, size, digest=None):
    """v3 PUT payload: u64 file_size, 32-byte hash, u16 name_len, name."""
    digest = digest if digest is not None else b"\0" * H.HASH_SIZE
    return (size.to_bytes(8, "big") + digest
            + len(name).to_bytes(2, "big") + name)
