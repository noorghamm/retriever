import socket
import threading
import time

import pytest

from retriever import protocol as H
from retriever.server import start_server


def _free_port():
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
    port = _free_port()
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


def _list(port):
    """Speak a raw LIST exchange per docs/PROTOCOL.md and return (status, names)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        H.send_u8(sock, 0)
        status = H.recv_u8(sock)
        length = H.recv_u64(sock)
        payload = H.read_exact_bytes(sock, length) if length else b""
    names = {n.decode("utf-8") for n in payload.split(b"\x00") if n}
    return status, names


def test_list_returns_directory_entries(server_port, tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)

    status, names = _list(server_port)

    assert status == 0
    assert names == {"a.txt", "b.png"}


def test_list_of_empty_directory(server_port):
    status, names = _list(server_port)

    assert status == 0
    assert names == set()


def test_failed_put_does_not_delete_existing_file(server_port, tmp_path):
    """A PUT that dies mid-transfer must not touch a pre-existing server file.

    Regression test: the v1 error handler removed `filename` on any
    exception, even when the failed upload never created that file.
    """
    original = b"\x89PNG\r\n\x1a\n" + b"original contents"
    existing = tmp_path / "photo.png"
    existing.write_bytes(original)

    with socket.create_connection(("127.0.0.1", server_port), timeout=5) as sock:
        name = b"photo.png"
        H.send_u8(sock, 2)              # PUT
        H.send_u16(sock, len(name))
        sock.sendall(name)
        H.send_u64(sock, 1000)          # promise a 1000-byte body...
    # ...and hang up without sending any of it

    time.sleep(0.3)  # let the server finish its error handling

    assert existing.exists(), "failed PUT deleted a file it never created"
    assert existing.read_bytes() == original
