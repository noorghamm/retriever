"""Shared fixtures and helpers for the retriever test suite."""
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


def name_payload(name, size=None):
    """Build a GET payload (u16 len + name) or PUT payload (+ u64 size)."""
    payload = len(name).to_bytes(2, "big") + name
    if size is not None:
        payload += size.to_bytes(8, "big")
    return payload
