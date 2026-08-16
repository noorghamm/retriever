"""Interop: the C client against the real Python server.

Two implementations of docs/PROTOCOL.md have to agree byte for byte, and
the only way to know is to make them talk to each other. These tests
build the C client from source and run it as a subprocess.
"""
import hashlib
import os
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
def c_build():
    """Build the C programs once per session, with sanitizers on.

    The sanitized build is the one worth testing: it turns latent memory
    errors into loud failures instead of silent corruption.
    """
    build = subprocess.run(["make", "-C", str(C_DIR), "debug"],
                           capture_output=True, text=True)
    if build.returncode != 0:
        pytest.fail(f"C build failed:\n{build.stdout}\n{build.stderr}")
    return C_DIR


@pytest.fixture(scope="session")
def c_client(c_build):
    return str(c_build / "retriever")


@pytest.fixture(scope="session")
def c_sha256(c_build):
    return str(c_build / "sha256tool")


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


def test_c_client_rejects_bad_usage(c_client, server_port):
    result = run_client(c_client, server_port, "frobnicate")

    assert result.returncode != 0
    assert "unknown command" in result.stderr


#--- the hand-written SHA-256 against Python's hashlib -----------------------

@pytest.mark.parametrize("size", [
    0, 1, 55, 56, 57, 63, 64, 65, 119, 120, 128, 1000, 65536, 1_048_577,
])
def test_c_sha256_matches_hashlib(c_sha256, size):
    """Sizes chosen around the padding boundaries: a block is 64 bytes and
    the length field claims the last 8, so 55/56 and 119/120 are where a
    padding bug shows up."""
    data = os.urandom(size)
    result = subprocess.run([c_sha256], input=data, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip() == hashlib.sha256(data).hexdigest()


#--- GET and PUT interop -----------------------------------------------------

def test_c_client_downloads_and_verifies(c_client, server_port, tmp_path):
    body = os.urandom(50_000)
    (tmp_path / "payload.bin").write_bytes(body)
    work = tmp_path / "clientside"
    work.mkdir()

    result = run_client(c_client, server_port, "get", "payload.bin",
                        "-o", str(work / "copy.bin"))

    assert result.returncode == 0, result.stderr
    assert (work / "copy.bin").read_bytes() == body
    assert "hash verified" in result.stdout


def test_c_client_uploads_and_server_verifies(c_client, server_port, tmp_path):
    work = tmp_path / "clientside"
    work.mkdir()
    body = os.urandom(40_000)
    (work / "up.bin").write_bytes(body)

    result = run_client(c_client, server_port, "put", str(work / "up.bin"))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "up.bin").read_bytes() == body


def test_c_upload_then_python_download(c_client, server_port, tmp_path):
    """A file that crossed the wire twice, once via each implementation,
    must come back byte-identical."""
    work = tmp_path / "clientside"
    work.mkdir()
    body = os.urandom(30_000)
    (work / "round.bin").write_bytes(body)

    up = run_client(c_client, server_port, "put", str(work / "round.bin"))
    assert up.returncode == 0, up.stderr

    with socket.create_connection(("127.0.0.1", server_port), timeout=10) as sock:
        H.write_frame(sock, H.T_HELLO)
        assert H.read_frame(sock)[0] == H.T_OK
        name = b"round.bin"
        H.write_frame(sock, H.T_GET,
                      (0).to_bytes(8, "big") + b"\0" * 8
                      + len(name).to_bytes(2, "big") + name)
        frame_type, payload_len = H.read_frame_header(sock)
        assert frame_type == H.T_OK
        meta = H.read_exact_bytes(sock, H.GET_META)
        got = H.read_exact_bytes(sock, payload_len - H.GET_META)

    assert got == body
    assert meta[16:] == hashlib.sha256(body).digest()


def test_c_client_refuses_to_overwrite_a_local_file(c_client, server_port, tmp_path):
    (tmp_path / "thing.bin").write_bytes(b"remote")
    work = tmp_path / "clientside"
    work.mkdir()
    (work / "thing.bin").write_bytes(b"precious local data")

    result = run_client(c_client, server_port, "get", "thing.bin",
                        "-o", str(work / "thing.bin"))

    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert (work / "thing.bin").read_bytes() == b"precious local data"


def test_c_client_reports_a_missing_file(c_client, server_port):
    result = run_client(c_client, server_port, "get", "nope.bin",
                        "-o", "/tmp/should-not-appear.bin")

    assert result.returncode != 0
    assert "not found" in result.stderr


def test_c_client_resumes_an_interrupted_download(c_client, server_port, tmp_path):
    """The full circle: a partial written by an interrupted transfer is
    picked up, completed, and hash-verified, all in C."""
    body = os.urandom(80_000)
    (tmp_path / "movie.bin").write_bytes(body)
    work = tmp_path / "clientside"
    work.mkdir()
    dest = work / "movie.bin"

    #stage a half-finished download by hand, named as the protocol requires
    digest = hashlib.sha256(body).digest()
    partial = work / H.partial_name("movie.bin", digest)
    partial.write_bytes(body[:30_000])

    result = run_client(c_client, server_port, "get", "movie.bin", "-o", str(dest))

    assert result.returncode == 0, result.stderr
    assert "resuming" in result.stdout
    assert "30000" in result.stdout
    assert dest.read_bytes() == body
    assert not partial.exists(), "partial should be renamed into place"


def test_c_client_discards_a_partial_for_a_different_file(c_client, server_port, tmp_path):
    """A stale partial must not be spliced onto the current file."""
    body = b"the real contents " * 100
    (tmp_path / "doc.bin").write_bytes(body)
    work = tmp_path / "clientside"
    work.mkdir()
    dest = work / "doc.bin"

    #a partial belonging to some other file entirely
    stale_digest = hashlib.sha256(b"a completely different file").digest()
    stale = work / H.partial_name("doc.bin", stale_digest)
    stale.write_bytes(b"garbage that must not survive")

    result = run_client(c_client, server_port, "get", "doc.bin", "-o", str(dest))

    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == body
    assert not stale.exists(), "stale partial should have been discarded"
