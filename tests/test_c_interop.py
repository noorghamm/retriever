"""Interop: the C client against the real Python server.

Two implementations of docs/PROTOCOL.md have to agree byte for byte, and
the only way to know is to make them talk to each other. These tests
build the C client from source and run it as a subprocess.
"""
import pathlib
import shutil
import socket
import subprocess
import threading

import pytest

from retriever import protocol as H

C_DIR = pathlib.Path(__file__).resolve().parent.parent / "c"

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("cc") is None,
    reason="needs make and a C compiler",
)


@pytest.fixture(scope="session")
def c_client():
    """Build the C client once per test session, with sanitizers on.

    The sanitized build is the one worth testing: it turns latent memory
    errors into loud failures instead of silent corruption.
    """
    build = subprocess.run(["make", "-C", str(C_DIR), "debug"],
                           capture_output=True, text=True)
    if build.returncode != 0:
        pytest.fail(f"C client failed to build:\n{build.stdout}\n{build.stderr}")
    return str(C_DIR / "retriever")


def run_client(c_client, port, *args, cwd=None):
    return subprocess.run([c_client, "127.0.0.1", str(port), *args],
                          capture_output=True, text=True, timeout=30, cwd=cwd)


def test_c_client_lists_the_servers_files(c_client, server_port, tmp_path):
    for name in ("alpha.txt", "beta.bin", "gamma.png"):
        (tmp_path / name).write_bytes(b"x")

    result = run_client(c_client, server_port, "list")

    assert result.returncode == 0, result.stderr
    assert sorted(result.stdout.split()) == ["alpha.txt", "beta.bin", "gamma.png"]


def test_c_client_handles_an_empty_directory(c_client, server_port):
    result = run_client(c_client, server_port, "list")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_c_and_python_clients_see_the_same_listing(c_client, server_port, tmp_path):
    """The real interop assertion: two implementations, one answer."""
    for name in ("one", "two", "three"):
        (tmp_path / name).write_text(name)

    c_result = run_client(c_client, server_port, "list")

    with socket.create_connection(("127.0.0.1", server_port), timeout=5) as sock:
        H.write_frame(sock, H.T_HELLO)
        assert H.read_frame(sock)[0] == H.T_OK
        H.write_frame(sock, H.T_LIST)
        _, payload = H.read_frame(sock)
    python_names = sorted(n.decode() for n in payload.split(b"\x00") if n)

    assert sorted(c_result.stdout.split()) == python_names


def test_c_client_reports_a_version_mismatch(c_client, tmp_path):
    """A server speaking an older version must produce a clear message,
    not a crash or a hang. This is the version byte earning its keep."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def old_server():
        conn, _ = listener.accept()
        try:
            conn.recv(64)                      # whatever the client said
            conn.sendall(H.MAGIC + bytes([2, H.T_OK]) + (0).to_bytes(8, "big"))
        except OSError:
            pass
        finally:
            conn.close()

    t = threading.Thread(target=old_server, daemon=True)
    t.start()

    result = run_client(c_client, port, "list")
    t.join(timeout=5)
    listener.close()

    assert result.returncode != 0
    assert "v2" in result.stderr and "v3" in result.stderr


def test_c_client_rejects_a_non_retriever_server(c_client):
    """Garbage on the port must be refused at the magic check."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def rude_server():
        conn, _ = listener.accept()
        try:
            conn.recv(64)
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        except OSError:
            pass
        finally:
            conn.close()

    t = threading.Thread(target=rude_server, daemon=True)
    t.start()

    result = run_client(c_client, port, "list")
    t.join(timeout=5)
    listener.close()

    assert result.returncode != 0
    assert "bad magic" in result.stderr


def test_c_client_refuses_an_unimplemented_command(c_client, server_port):
    result = run_client(c_client, server_port, "get")

    assert result.returncode != 0
    assert "only 'list'" in result.stderr
